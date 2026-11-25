import AVFoundation
import CoreAudio
import CoreGraphics
import CoreMedia
import Darwin
import ScreenCaptureKit

enum MeetingSystemAudioMode: String {
    case screen, tap, tapAndScreen

    var backends: [MeetingSystemAudioMode] {
        self == .tapAndScreen ? [.tap, .screen] : [self]
    }
}

private func captureHostBundleID() -> String? {
    let bundleURL = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    return Bundle(url: bundleURL)?.bundleIdentifier
}

func openScreenSystemAudio(delegate: SCStreamDelegate, output: SCStreamOutput, queue: DispatchQueue) async throws -> SCStream {
    let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: false)
    guard let display = content.displays.first else { throw CaptureError("No Mac display is available for system audio capture.") }
    let parentBundle = captureHostBundleID()
    let excluded = content.applications.filter { parentBundle != nil && $0.bundleIdentifier == parentBundle }
    let filter = SCContentFilter(display: display, excludingApplications: excluded, exceptingWindows: [])
    let config = SCStreamConfiguration()
    config.width = 2; config.height = 2
    config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
    config.queueDepth = 3
    config.showsCursor = false
    config.capturesAudio = true
    config.excludesCurrentProcessAudio = true
    config.sampleRate = Timeline.rate
    config.channelCount = 2
    let stream = SCStream(filter: filter, configuration: config, delegate: delegate)
    // ScreenCaptureKit may require a screen output. Only audio callbacks are read.
    try stream.addStreamOutput(output, type: .screen, sampleHandlerQueue: queue)
    try stream.addStreamOutput(output, type: .audio, sampleHandlerQueue: queue)
    try await stream.startCapture()
    return stream
}

func withScreenAudio(_ sample: CMSampleBuffer, _ receive: (AVAudioPCMBuffer, Double) throws -> Void) throws {
    guard sample.isValid, sample.dataReadiness == .ready, sample.numSamples > 0 else { return }
    guard let description = sample.formatDescription,
          let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(description),
          let format = AVAudioFormat(streamDescription: asbd) else {
        throw CaptureError("Mac audio returned an unknown audio format.")
    }
    try sample.withAudioBufferList { buffers, _ in
        guard let pcm = AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: buffers.unsafePointer) else {
            throw CaptureError("Could not read Mac audio samples.")
        }
        pcm.frameLength = AVAudioFrameCount(sample.numSamples)
        try receive(pcm, sample.presentationTimeStamp.seconds)
    }
}

private struct AudioTapError: LocalizedError {
    let action: String
    let status: OSStatus
    var errorDescription: String? { "Could not \(action) for Mac audio (\(status))." }
}

private func checkAudioTap(_ status: OSStatus, _ action: String) throws {
    if status != noErr { throw AudioTapError(action: action, status: status) }
}

// Startup hands ownership to the stop worker. The audio callback doesn't access these handles.
private final class MeetingAudioTap: @unchecked Sendable {
    private var tap: AudioObjectID = 0
    private var device: AudioObjectID = 0
    private var callback: AudioDeviceIOProcID?
    private var running = false

    private static func audioProcess(_ pid: pid_t) -> AudioObjectID? {
        var pid = pid
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
            mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var id: AudioObjectID = 0
        var size = UInt32(MemoryLayout.size(ofValue: id))
        return AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address,
                                          UInt32(MemoryLayout.size(ofValue: pid)), &pid, &size, &id) == noErr && id != 0 ? id : nil
    }

    init(hostPID: pid_t?, deliver: @escaping (UnsafePointer<AudioBufferList>, AudioTimeStamp, AVAudioFormat) -> Void) throws {
        let excluded = [getpid(), getppid(), hostPID].compactMap { $0 }.compactMap(Self.audioProcess)
        let description = CATapDescription(monoGlobalTapButExcludeProcesses: excluded)
        description.name = "Jaro Meeting Audio"
        description.isPrivate = true
        description.muteBehavior = .unmuted
        if #available(macOS 26.0, *), let bundle = captureHostBundleID() {
            description.bundleIDs = [bundle]
            description.isProcessRestoreEnabled = true
        }
        do {
            try checkAudioTap(AudioHardwareCreateProcessTap(description, &tap), "create a process tap")
            var address = AudioObjectPropertyAddress(mSelector: kAudioTapPropertyFormat,
                mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
            var asbd = AudioStreamBasicDescription()
            var size = UInt32(MemoryLayout.size(ofValue: asbd))
            try checkAudioTap(AudioObjectGetPropertyData(tap, &address, 0, nil, &size, &asbd), "get the tap format")
            guard let format = AVAudioFormat(streamDescription: &asbd), format.commonFormat == .pcmFormatFloat32,
                  format.channelCount == 1, format.sampleRate.isFinite, format.sampleRate > 0 else {
                throw CaptureError("Mac audio returned an unsupported process tap format.")
            }
            let aggregate: [String: Any] = [
                kAudioAggregateDeviceNameKey: "Jaro Meeting Audio",
                kAudioAggregateDeviceUIDKey: "local.jardaudio.meeting." + UUID().uuidString,
                kAudioAggregateDeviceIsPrivateKey: true,
                kAudioAggregateDeviceTapAutoStartKey: true,
                kAudioAggregateDeviceTapListKey: [[
                    kAudioSubTapUIDKey: description.uuid.uuidString,
                    kAudioSubTapDriftCompensationKey: true,
                ]],
            ]
            try checkAudioTap(AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &device), "create a tap device")
            try checkAudioTap(AudioDeviceCreateIOProcIDWithBlock(&callback, device, nil) { _, input, time, _, _ in
                deliver(input, time.pointee, format)
            }, "attach the tap callback")
            try checkAudioTap(AudioDeviceStart(device, callback), "start the process tap")
            running = true
        } catch {
            stop()
            throw error
        }
    }

    func stop() {
        if let callback {
            if running { AudioDeviceStop(device, callback) }
            AudioDeviceDestroyIOProcID(device, callback)
            self.callback = nil
            running = false
        }
        if device != 0 { AudioHardwareDestroyAggregateDevice(device); device = 0 }
        if tap != 0 { AudioHardwareDestroyProcessTap(tap); tap = 0 }
    }

    deinit { stop() }
}

private func defaultOutputUID() -> String? {
    defaultAudioDevice(kAudioHardwarePropertyDefaultOutputDevice).flatMap { audioDeviceString(kAudioDevicePropertyDeviceUID, of: $0) }
}

// Only the process tap binds to the default output. Check it from the supervisor, outside audio callbacks.
struct MeetingOutputRoute {
    private var original: String?
    private var nextCheck: Double

    init(at now: Double, uid: String?) {
        original = uid?.isEmpty == false ? uid : nil
        nextCheck = now + 2
    }

    mutating func changed(at now: Double, read: () -> String?) -> Bool {
        guard now >= nextCheck else { return false }
        nextCheck = now + 2
        guard let current = read(), !current.isEmpty else { return false }
        guard let original else { self.original = current; return false }
        return current != original
    }
}

enum MeetingSystemAudioReadiness {
    case waiting, ready, failed, permissionDenied
}

struct MeetingSystemAudioHealth {
    var openedAt: Double
    var failed = false
    var permissionDenied = false
    private var lastPacketAt: Double?
    // MonoConverter delivers Timeline.rate frames per second. Silence before the first signal is normal.
    private var zeroFramesAfterSignal: Int?
    private static let silenceLimit = 15 * Timeline.rate

    init(openedAt: Double) { self.openedAt = openedAt }

    func readiness(for backend: MeetingSystemAudioMode, at now: Double) -> MeetingSystemAudioReadiness {
        if permissionDenied { return .permissionDenied }
        if failed || (zeroFramesAfterSignal ?? 0) >= Self.silenceLimit { return .failed }
        // ScreenCaptureKit can pause audio callbacks while the Mac is quiet.
        if backend == .screen { return .ready }
        if now - (lastPacketAt ?? openedAt) >= 3 { return .failed }
        return lastPacketAt == nil ? .waiting : .ready
    }

    mutating func receive(_ samples: [Float], timestamp: Double, at now: Double) -> Bool {
        guard !samples.isEmpty else { return false }
        guard timestamp.isFinite, abs(timestamp - now) < 10 else { failed = true; return false }
        if let last = samples.lastIndex(where: { $0 != 0 }) {
            zeroFramesAfterSignal = samples.count - last - 1
        } else if let previous = zeroFramesAfterSignal {
            zeroFramesAfterSignal = min(Self.silenceLimit, previous + samples.count)
        }
        lastPacketAt = now
        return true
    }
}

protocol MeetingSystemAudioConnection {
    var readiness: MeetingSystemAudioReadiness { get }
    func stop() async
}

typealias MeetingSystemAudioOpener = (MeetingSystemAudioMode, pid_t?, @escaping ([Float], Double) -> Void) async throws
    -> any MeetingSystemAudioConnection

private final class MeetingSystemAudioSlots: @unchecked Sendable {
    static let shared = MeetingSystemAudioSlots()
    private let lock = NSLock()
    private var occupied: Set<MeetingSystemAudioMode> = []

    func reserve(_ backend: MeetingSystemAudioMode) -> Bool { lock.withLock { occupied.insert(backend).inserted } }
    func isReserved(_ backend: MeetingSystemAudioMode) -> Bool { lock.withLock { occupied.contains(backend) } }
    func release(_ backend: MeetingSystemAudioMode) { lock.withLock { _ = occupied.remove(backend) } }
}

private enum MeetingSystemAudioOpenResult {
    case connected(any MeetingSystemAudioConnection), unavailable, permissionDenied
}

private final class MeetingSystemAudioOpen: @unchecked Sendable {
    private typealias Reply = CheckedContinuation<MeetingSystemAudioOpenResult, Never>
    private enum State {
        case starting, waiting(Reply), active, retired
    }
    private let lock = NSLock()
    private var state = State.starting

    private func wait(_ reply: Reply) -> Bool {
        lock.withLock {
            guard case .starting = state else { return false }
            state = .waiting(reply)
            return true
        }
    }

    @discardableResult
    private func resolve(_ result: MeetingSystemAudioOpenResult) -> Bool {
        let reply: Reply? = lock.withLock {
            if case .starting = state { state = .retired }
            guard case let .waiting(reply) = state else { return nil }
            if case .connected = result { state = .active }
            else { state = .retired }
            return reply
        }
        reply?.resume(returning: result)
        return reply != nil
    }

    func retire() { lock.withLock { state = .retired } }

    private func deliver(_ work: () -> Void) {
        lock.withLock { if case .active = state { work() } }
    }

    func run(backend: MeetingSystemAudioMode, hostPID: pid_t?, timeout: Duration,
             open: @escaping MeetingSystemAudioOpener,
             receive: @escaping ([Float], Double) -> Void) async -> MeetingSystemAudioOpenResult {
        let slots = MeetingSystemAudioSlots.shared
        guard slots.reserve(backend) else { return .unavailable }
        let deadline = ContinuousClock.now.advanced(by: timeout)
        let timer = Task {
            do {
                try await Task.sleep(until: deadline, clock: .continuous)
                self.resolve(.unavailable)
            } catch is CancellationError {}
        }
        defer { timer.cancel() }
        return await withTaskCancellationHandler {
            await withCheckedContinuation { reply in
                guard wait(reply) else {
                    slots.release(backend)
                    reply.resume(returning: .unavailable)
                    return
                }
                // macOS can block in an open that Swift cannot cancel. The worker keeps
                // the backend slot until a late connection has been stopped.
                Task.detached {
                    do {
                        let source = try await open(backend, hostPID) { samples, time in
                            self.deliver { receive(samples, time) }
                        }
                        if ContinuousClock.now < deadline && self.resolve(.connected(source)) { return }
                        self.resolve(.unavailable)
                        await source.stop()
                    } catch {
                        slots.release(backend)
                        self.resolve(isSystemCaptureDenial(error) ? .permissionDenied : .unavailable)
                        return
                    }
                    slots.release(backend)
                }
            }
        } onCancel: {
            self.resolve(.unavailable)
        }
    }
}

// The supervisor serializes start and stop. Audio callbacks use queue and healthLock.
private final class MeetingSystemAudioSource: NSObject, SCStreamDelegate, SCStreamOutput, MeetingSystemAudioConnection, @unchecked Sendable {
    private let queue = DispatchQueue(label: "jard.capture.meeting-system")
    private let pending = DispatchSemaphore(value: 48)
    private let healthLock = NSLock()
    private let backend: MeetingSystemAudioMode
    private var health = MeetingSystemAudioHealth(openedAt: hostSeconds())
    private let converter = MonoConverter()
    private let receive: ([Float], Double) -> Void
    private var screen: SCStream?
    private var tap: MeetingAudioTap?
    private var outputRoute: MeetingOutputRoute?

    init(backend: MeetingSystemAudioMode, receive: @escaping ([Float], Double) -> Void) {
        self.backend = backend
        self.receive = receive
    }

    func start(hostPID: pid_t?) async throws {
        if backend == .tap {
            outputRoute = MeetingOutputRoute(at: hostSeconds(), uid: defaultOutputUID())
            tap = try await withCheckedThrowingContinuation { continuation in
                DispatchQueue.global(qos: .userInitiated).async {
                    continuation.resume(with: Result {
                        try MeetingAudioTap(hostPID: hostPID) { [weak self] input, time, format in
                            self?.receiveTap(input, at: time, format: format)
                        }
                    })
                }
            }
        } else {
            screen = try await openScreenSystemAudio(delegate: self, output: self, queue: queue)
        }
        healthLock.withLock { health.openedAt = hostSeconds() }
    }

    func stop() async {
        if let screen {
            try? await screen.stopCapture()
            self.screen = nil
        }
        if let tap {
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                DispatchQueue.global(qos: .userInitiated).async {
                    tap.stop()
                    continuation.resume()
                }
            }
            self.tap = nil
        }
        queue.sync { try? converter.finish(emit: forward) }
    }

    var readiness: MeetingSystemAudioReadiness {
        let now = hostSeconds()
        if outputRoute?.changed(at: now, read: defaultOutputUID) == true { return .failed }
        return healthLock.withLock { health.readiness(for: backend, at: now) }
    }

    private func fail() { healthLock.withLock { health.failed = true } }

    private func forward(_ samples: [Float], at timestamp: Double) {
        guard healthLock.withLock({ health.receive(samples, timestamp: timestamp, at: hostSeconds()) }) else { return }
        receive(samples, timestamp)
    }

    private func receiveTap(_ input: UnsafePointer<AudioBufferList>, at time: AudioTimeStamp, format: AVAudioFormat) {
        guard input.pointee.mNumberBuffers > 0 else { return }
        let buffer = input.pointee.mBuffers
        let count = Int(buffer.mDataByteSize) / MemoryLayout<Float>.size
        guard count > 0, count <= 65_536, let data = buffer.mData else { return }
        guard pending.wait(timeout: .now()) == .success else { return }
        let floats = Array(UnsafeBufferPointer(start: data.assumingMemoryBound(to: Float.self), count: count))
        let capturedAt = time.mFlags.contains(.hostTimeValid)
            ? AVAudioTime.seconds(forHostTime: time.mHostTime) : hostSeconds() - Double(count) / format.sampleRate
        queue.async {
            defer { self.pending.signal() }
            guard let pcm = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(count)),
                  let samples = pcm.floatChannelData else { self.fail(); return }
            pcm.frameLength = AVAudioFrameCount(count)
            floats.withUnsafeBufferPointer { samples[0].update(from: $0.baseAddress!, count: count) }
            do { try self.converter.append(pcm, at: capturedAt, emit: self.forward) }
            catch { self.fail() }
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        healthLock.withLock {
            health.failed = true
            if isSystemCaptureDenial(error) { health.permissionDenied = true }
        }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sample: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio else { return }
        do {
            try withScreenAudio(sample) { pcm, time in try converter.append(pcm, at: time, emit: forward) }
        } catch { fail() }
    }
}

func openMeetingSystemAudio(_ backend: MeetingSystemAudioMode, hostPID: pid_t?,
                            receive: @escaping ([Float], Double) -> Void) async throws -> any MeetingSystemAudioConnection {
    let source = MeetingSystemAudioSource(backend: backend, receive: receive)
    do {
        try await source.start(hostPID: hostPID)
        return source
    } catch {
        await source.stop()
        throw error
    }
}

enum MeetingSystemAudioRetry {
    case elapsed, available, cancelled
}

private func meetingConsoleAvailable() -> Bool {
    guard let session = CGSessionCopyCurrentDictionary() as? [String: Any],
          session[kCGSessionOnConsoleKey as String] as? Bool == true else { return false }
    return session["CGSSessionScreenIsLocked"] as? Bool != true && CGDisplayIsAsleep(CGMainDisplayID()) == 0
}

func waitForMeetingSystemAudioRetry(_ delay: Duration, becameAvailable: () -> Bool) async -> MeetingSystemAudioRetry {
    let deadline = ContinuousClock.now.advanced(by: delay)
    while !Task.isCancelled {
        if becameAvailable() { return .available }
        let remaining = ContinuousClock.now.duration(to: deadline)
        if remaining <= .zero { return .elapsed }
        do { try await Task.sleep(for: min(remaining, .milliseconds(250))) }
        catch { return .cancelled }
    }
    return .cancelled
}

func superviseMeetingSystemAudio(mode: MeetingSystemAudioMode, hostPID: pid_t?,
                                 receive: @escaping (UUID, [Float], Double) -> Void,
                                 change: (Bool, MeetingSystemAudioMode?) -> Void,
                                 permissionDenied: () -> Void = {},
                                 open: @escaping MeetingSystemAudioOpener = openMeetingSystemAudio,
                                 openTimeout: Duration = .seconds(3),
                                 pause: (Duration) async -> Bool = { duration in
                                     do { try await Task.sleep(for: duration); return true }
                                     catch { return false }
                                 },
                                 consoleAvailable: @escaping () -> Bool = meetingConsoleAvailable,
                                 waitForRetry: (Duration, () -> Bool) async -> MeetingSystemAudioRetry
                                     = waitForMeetingSystemAudioRetry) async {
    var delay = 1
    var unavailable = false
    let slots = MeetingSystemAudioSlots.shared
    func reportUnavailable() {
        if !unavailable { change(false, nil); unavailable = true }
    }
    while !Task.isCancelled {
        let consoleWasAvailable = consoleAvailable()
        var blocked = [MeetingSystemAudioMode]()
        for backend in mode.backends {
            if slots.isReserved(backend) {
                blocked.append(backend)
                continue
            }
            let id = UUID()
            let attempt = MeetingSystemAudioOpen()
            let result = await attempt.run(backend: backend, hostPID: hostPID, timeout: openTimeout, open: open,
                                           receive: { samples, time in receive(id, samples, time) })
            guard case let .connected(source) = result else {
                if Task.isCancelled { return }
                if case .permissionDenied = result { permissionDenied(); return }
                reportUnavailable()
                continue
            }
            var readyAt: Double?
            var failure: MeetingSystemAudioReadiness?
            while !Task.isCancelled {
                let readiness = source.readiness
                if readiness == .failed || readiness == .permissionDenied {
                    failure = readiness
                    break
                }
                if readiness == .ready && readyAt == nil {
                    readyAt = hostSeconds()
                    change(true, backend)
                    unavailable = false
                }
                if !(await pause(.milliseconds(250))) { break }
            }
            if failure == nil || Task.isCancelled {
                await source.stop()
                attempt.retire()
                slots.release(backend)
                return
            }
            let stable = readyAt.map { hostSeconds() - $0 >= 6 } ?? false
            attempt.retire()
            Task.detached {
                await source.stop()
                slots.release(backend)
            }
            if failure == .permissionDenied { permissionDenied(); return }
            reportUnavailable()
            if stable { delay = 1 }
        }
        reportUnavailable()
        let retry = await waitForRetry(.seconds(delay)) {
            (!consoleWasAvailable && consoleAvailable()) || blocked.contains { !slots.isReserved($0) }
        }
        switch retry {
        case .cancelled: return
        case .available:
            if !consoleWasAvailable && consoleAvailable() { delay = 1 }
        case .elapsed:
            delay = min(30, delay * 2)
        }
    }
}
