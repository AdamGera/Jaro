import AppKit
import AVFoundation
import CoreAudio
import CoreMedia
import Darwin
import IOKit
import ScreenCaptureKit

struct RecordingPermissionError: LocalizedError {
    let source: Source
    var errorDescription: String? {
        source == .mic ? "macOS has not allowed microphone access." : "macOS has not allowed Screen & System Audio Recording."
    }
}

func isSystemCaptureDenial(_ error: Error) -> Bool {
    var pending = [error as NSError]
    var visited = Set<ObjectIdentifier>()
    while let cause = pending.popLast() {
        guard visited.insert(ObjectIdentifier(cause)).inserted else { continue }
        if cause.domain == SCStreamErrorDomain && cause.code == SCStreamError.Code.userDeclined.rawValue { return true }
        if let underlying = cause.userInfo[NSUnderlyingErrorKey] as? NSError { pending.append(underlying) }
        if let causes = cause.userInfo[NSMultipleUnderlyingErrorsKey] as? [NSError] { pending.append(contentsOf: causes) }
    }
    return false
}

func audioDeviceString(_ selector: AudioObjectPropertySelector, of id: AudioDeviceID) -> String? {
    var address = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var value: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout.size(ofValue: value))
    guard AudioObjectGetPropertyData(id, &address, 0, nil, &size, &value) == noErr, let value else { return nil }
    return value.takeRetainedValue() as String
}

func defaultAudioDevice(_ selector: AudioObjectPropertySelector) -> AudioDeviceID? {
    var address = AudioObjectPropertyAddress(mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var id = AudioDeviceID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout.size(ofValue: id))
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &id) == noErr,
          id != kAudioObjectUnknown else { return nil }
    return id
}

struct InputDevice: Equatable {
    let id: AudioDeviceID
    let name: String
    let uid: String
    let transport: UInt32
    var inputDataSource: UInt32? = nil

    var isInternalMicrophone: Bool {
        // Older Macs expose a headphone jack as the built-in input's emic or line data source.
        transport == kAudioDeviceTransportTypeBuiltIn && !uid.contains("HeadphoneInput") &&
            inputDataSource != 0x656d6963 && inputDataSource != 0x6c696e65
    }

    var isBluetooth: Bool {
        transport == kAudioDeviceTransportTypeBluetooth || transport == kAudioDeviceTransportTypeBluetoothLE
    }

    var canStandInForSystemInput: Bool {
        switch transport {
        case kAudioDeviceTransportTypeVirtual, kAudioDeviceTransportTypeAggregate, kAudioDeviceTransportTypeAutoAggregate,
             kAudioDeviceTransportTypeContinuityCaptureWired, kAudioDeviceTransportTypeContinuityCaptureWireless,
             0x63636170: // The original Continuity transport, ccap.
            return false
        default:
            return id != 0
        }
    }

    static let unavailable = Self(id: 0, name: "No microphone", uid: "", transport: 0)

    private static func describe(_ id: AudioDeviceID) -> Self {
        var address = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyTransportType, mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var transport: UInt32 = 0
        var size = UInt32(MemoryLayout.size(ofValue: transport))
        _ = AudioObjectGetPropertyData(id, &address, 0, nil, &size, &transport)
        var inputSource: UInt32?
        if transport == kAudioDeviceTransportTypeBuiltIn {
            address.mSelector = kAudioDevicePropertyDataSource
            address.mScope = kAudioObjectPropertyScopeInput
            var value: UInt32 = 0
            var valueSize = UInt32(MemoryLayout<UInt32>.size)
            if AudioObjectGetPropertyData(id, &address, 0, nil, &valueSize, &value) == noErr { inputSource = value }
        }
        return Self(id: id, name: audioDeviceString(kAudioObjectPropertyName, of: id) ?? "Mac microphone",
                    uid: audioDeviceString(kAudioDevicePropertyDeviceUID, of: id) ?? "", transport: transport, inputDataSource: inputSource)
    }

    static func current() -> Self {
        defaultAudioDevice(kAudioHardwarePropertyDefaultInputDevice).map(describe) ?? .unavailable
    }

    static func available() -> [Self] {
        let system = AudioObjectID(kAudioObjectSystemObject)
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(system, &address, 0, nil, &size) == noErr,
              size >= MemoryLayout<AudioDeviceID>.stride else { return [] }
        var devices = Array(repeating: AudioDeviceID(0), count: Int(size) / MemoryLayout<AudioDeviceID>.stride)
        guard devices.withUnsafeMutableBytes({ buffer in
            AudioObjectGetPropertyData(system, &address, 0, nil, &size, buffer.baseAddress!)
        }) == noErr else { return [] }
        return devices.compactMap { id in
            var streams = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams,
                mScope: kAudioDevicePropertyScopeInput, mElement: kAudioObjectPropertyElementMain)
            var bytes: UInt32 = 0
            guard id != 0, AudioObjectGetPropertyDataSize(id, &streams, 0, nil, &bytes) == noErr, bytes > 0 else { return nil }
            let device = describe(id)
            return device.uid.isEmpty ? nil : device
        }.sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
    }

    func currentConnection() -> Self? {
        Self.available().first { $0.id == id && (uid.isEmpty || $0.uid == uid) }
    }

}

enum MicrophoneLid {
    static let unavailableMessage = "The built-in microphone is off while the lid is closed. Open the lid or connect a microphone."
    private static let powerRoot = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOPMrootDomain"))

    static var closed: Bool {
        #if JARD_CAPTURE_FIXTURE
        if let path = ProcessInfo.processInfo.environment["JARD_TEST_MICROPHONE_LID_GATE"] {
            return FileManager.default.fileExists(atPath: path)
        }
        return false
        #else
        guard powerRoot != 0 else { return false }
        let state = IORegistryEntryCreateCFProperty(powerRoot, "AppleClamshellState" as CFString, kCFAllocatorDefault, 0)
        return state?.takeRetainedValue() as? Bool == true
        #endif
    }
}

struct DictationInputRoute: Equatable {
    let saved: InputDevice?
    let system: InputDevice
    let lidBlocksInternalInput: Bool

    var primary: InputDevice { saved ?? system }
    var hasFallback: Bool { saved != nil && system.id != 0 && system.id != saved?.id }

    static func current(savedUID: String?, avoidBluetooth: Bool) -> Self {
        let systemDefault = InputDevice.current()
        let lidClosed = MicrophoneLid.closed
        if savedUID == nil && systemDefault.id != 0 && !(lidClosed && systemDefault.isInternalMicrophone) &&
            (!avoidBluetooth || !systemDefault.isBluetooth) {
            return Self(saved: nil, system: systemDefault, lidBlocksInternalInput: false)
        }
        return resolve(savedUID: savedUID, available: InputDevice.available(), systemDefault: systemDefault,
                       lidClosed: lidClosed, avoidBluetooth: avoidBluetooth)
    }

    static func resolve(savedUID: String?, available: [InputDevice], systemDefault: InputDevice,
                        lidClosed: Bool, avoidBluetooth: Bool) -> Self {
        let blocked: (InputDevice) -> Bool = { lidClosed && $0.isInternalMicrophone }
        let availableInputs = available.filter { $0.id != 0 && !blocked($0) }
        let saved = savedUID.flatMap { uid in availableInputs.first { $0.uid == uid } }
        let defaultUsable = systemDefault.id != 0 && !blocked(systemDefault)
        let standIns = availableInputs.filter { $0.canStandInForSystemInput }
        let system: InputDevice
        if defaultUsable && (!avoidBluetooth || !systemDefault.isBluetooth) {
            system = systemDefault
        } else {
            system = standIns.first(where: { $0.isInternalMicrophone })
                ?? standIns.first(where: { !avoidBluetooth || !$0.isBluetooth })
                ?? (defaultUsable ? systemDefault : standIns.first ?? .unavailable)
        }
        return Self(saved: saved, system: system,
                    lidBlocksInternalInput: blocked(systemDefault) || available.contains(where: blocked))
    }

    func require(_ input: InputDevice) throws -> InputDevice {
        guard input.id != 0 else {
            throw CaptureError(lidBlocksInternalInput ? MicrophoneLid.unavailableMessage : "Choose an input in System Settings → Sound.")
        }
        return input
    }

    func openRecordingInput(keeping current: InputDevice? = nil, open: (InputDevice) throws -> Void) throws -> InputDevice {
        let selected = try require(primary)
        if selected == current { return selected }
        do {
            try open(selected)
            return selected
        } catch let denial as RecordingPermissionError {
            throw denial
        } catch where hasFallback {
            if system != current { try open(system) }
            return system
        }
    }
}

enum ColdMicrophoneOpen: Equatable {
    case selected, systemDefault, abandoned
}

func openColdMicrophone(selected: (() -> InputDevice)?, systemDefault: () throws -> InputDevice,
                        stopped: () -> Bool, open: (InputDevice) throws -> Void,
                        pause: (Duration) async throws -> Void = { try await Task.sleep(for: $0) }) async throws -> ColdMicrophoneOpen {
    guard !stopped() else { return .abandoned }
    let preferred = selected?()
    if let selected, let preferred, preferred.id != 0 {
        for attempt in 0..<3 {
            let device = attempt == 0 ? preferred : selected()
            guard !stopped() else { return .abandoned }
            do {
                try open(device)
                return stopped() ? .abandoned : .selected
            } catch let permission as RecordingPermissionError {
                throw permission
            } catch {
                guard !stopped() else { return .abandoned }
                if attempt < 2 { try await pause(.milliseconds(100)) }
            }
        }
    }
    guard !stopped() else { return .abandoned }
    let fallback = try systemDefault()
    guard !stopped() else { return .abandoned }
    try open(fallback)
    return stopped() ? .abandoned : .systemDefault
}

private let eventOutputLock = NSLock()

func emit(_ fields: [String: Any]) {
    guard var data = try? JSONSerialization.data(withJSONObject: fields) else { return }
    data.append(10)
    eventOutputLock.lock()
    defer { eventOutputLock.unlock() }
    try? FileHandle.standardOutput.write(contentsOf: data)
}

func hostSeconds() -> Double { CMClockGetTime(CMClockGetHostTimeClock()).seconds }

/// One instance per input stream. AVAudioConverter retains its sample-rate conversion
/// state across callbacks when a Bluetooth device delivers 16/24/44.1 kHz input.
final class MonoConverter {
    private var converter: AVAudioConverter?
    private let mono = AVAudioFormat(standardFormatWithSampleRate: Double(Timeline.rate), channels: 1)!

    // AVAudioConverter can hold part of a packet and emit it on a later call.
    // Track that audio in output frames so its timestamp travels with the samples.
    private let lock = NSLock()
    private var closed = false
    private var bufferedFrames = 0.0
    private var inputEnd: Double?

    func append(_ input: AVAudioPCMBuffer, at timestamp: Double,
                emit: ([Float], Double) -> Void) throws {
        lock.lock()
        defer { lock.unlock() }
        guard !closed else { return }
        guard timestamp.isFinite else { throw CaptureError("Audio input lost its timing reference.") }
        guard input.frameLength > 0 else { return }
        let rate = input.format.sampleRate
        guard rate > 0 else { throw CaptureError("Unsupported audio input format.") }
        if converter?.inputFormat != input.format ||
            inputEnd.map({ abs(timestamp - $0) > 0.002 }) == true {
            try drain(emit: emit)
            converter = AVAudioConverter(from: input.format, to: mono)
            converter?.downmix = true
            converter?.primeMethod = .none
        }
        let duration = Double(input.frameLength) / rate
        let start = timestamp - bufferedFrames / mono.sampleRate
        let frames = duration * mono.sampleRate
        let output = try convert(input, capacity: Int(ceil(frames)) + 32)
        bufferedFrames += frames - Double(output.count)
        inputEnd = timestamp + duration
        if !output.isEmpty { emit(output, start) }
    }

    func finish(emit: ([Float], Double) -> Void) throws {
        lock.lock()
        defer { lock.unlock() }
        closed = true
        try drain(emit: emit)
    }

    private func drain(emit: ([Float], Double) -> Void) throws {
        defer { converter = nil; inputEnd = nil; bufferedFrames = 0 }
        guard let inputEnd else { return }
        while bufferedFrames.rounded() > 0 {
            let start = inputEnd - bufferedFrames / mono.sampleRate
            let output = try convert(nil, capacity: Int(bufferedFrames.rounded()))
            if output.isEmpty { break }
            emit(output, start)
            bufferedFrames -= Double(output.count)
        }
    }

    private func convert(_ input: AVAudioPCMBuffer?, capacity: Int) throws -> [Float] {
        guard let converter,
              let out = AVAudioPCMBuffer(pcmFormat: mono, frameCapacity: AVAudioFrameCount(capacity)) else {
            throw CaptureError("Could not initialize audio conversion.")
        }
        var provided = false
        var error: NSError?
        let status = converter.convert(to: out, error: &error) { _, state in
            guard let input else { state.pointee = .endOfStream; return nil }
            if provided { state.pointee = .noDataNow; return nil }
            provided = true
            state.pointee = .haveData
            return input
        }
        if let error { throw error }
        guard status != .error, let data = out.floatChannelData else { throw CaptureError("Audio conversion failed.") }
        return Array(UnsafeBufferPointer(start: data[0], count: Int(out.frameLength)))
    }
}

func openMicrophone(_ selected: InputDevice, pinDevice: Bool = false,
                    receive: @escaping (AVAudioPCMBuffer, AVAudioTime) -> Void) throws -> AVAudioEngine {
    guard selected.id != 0 else { throw CaptureError("The selected microphone is unavailable.") }
    let instance = AVAudioEngine()
    let node = instance.inputNode
    if pinDevice || selected.id != InputDevice.current().id {
        guard let unit = node.audioUnit else { throw CaptureError("The selected microphone could not be opened.") }
        var id = selected.id
        let status = AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global,
                                          0, &id, UInt32(MemoryLayout.size(ofValue: id)))
        guard status == noErr else { throw CaptureError("The selected microphone could not be opened.") }
    }
    let format = node.outputFormat(forBus: 0)
    guard format.channelCount > 0, format.sampleRate > 0 else { throw CaptureError("The microphone is unavailable.") }
    node.installTap(onBus: 0, bufferSize: 1024, format: nil, block: receive)
    do {
        instance.prepare()
        try instance.start()
        return instance
    } catch {
        node.removeTap(onBus: 0)
        instance.stop()
        throw error
    }
}

func alternateMicrophoneRate(from current: Double, supported ranges: [AudioValueRange]) -> Double? {
    let preferred: [Double] = current == 48_000 ? [44_100, 48_000] : [48_000, 44_100]
    let candidates = preferred + ranges.flatMap { [$0.mMinimum, $0.mMaximum] }
    return candidates.first { rate in
        rate.isFinite && rate > 0 && abs(rate - current) > 0.5 &&
            ranges.contains { $0.mMinimum <= rate && rate <= $0.mMaximum }
    }
}

enum MicrophoneClockReset: String {
    case reset, unavailable, failed, restoreFailed
}

func cycleMicrophoneClock(original: Double, alternate: Double, set: (Double) -> Bool,
                          settled: (Double) -> Bool, prime: () -> Bool) -> MicrophoneClockReset {
    guard set(alternate) else { return .failed }
    let primed = settled(alternate) && prime()
    for _ in 0..<2 {
        if set(original) && settled(original) { return primed ? .reset : .failed }
    }
    return .restoreFailed
}

func microphoneClockIdle(readRunning: () -> UInt32?, pause: () -> Void) -> Bool {
    for _ in 0..<5 {
        guard let running = readRunning() else { return false }
        if running == 0 { return true }
        pause()
    }
    return false
}

func resetMicrophoneClock(_ device: InputDevice, allowActiveDevice: Bool = false) -> MicrophoneClockReset {
    func property(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                   mElement: kAudioObjectPropertyElementMain)
    }
    var running = property(kAudioDevicePropertyDeviceIsRunningSomewhere)
    func runningOnDevice() -> UInt32? {
        var inUse: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        return AudioObjectGetPropertyData(device.id, &running, 0, nil, &size, &inUse) == noErr ? inUse : nil
    }
    guard device.id != 0, allowActiveDevice ||
          microphoneClockIdle(readRunning: runningOnDevice, pause: { Thread.sleep(forTimeInterval: 0.01) }) else {
        return .unavailable
    }
    var nominal = property(kAudioDevicePropertyNominalSampleRate)
    var settable = DarwinBoolean(false)
    guard AudioObjectIsPropertySettable(device.id, &nominal, &settable) == noErr, settable.boolValue else { return .unavailable }

    func currentRate() -> Double? {
        var rate: Double = 0
        var size = UInt32(MemoryLayout<Double>.size)
        guard AudioObjectGetPropertyData(device.id, &nominal, 0, nil, &size, &rate) == noErr, rate.isFinite, rate > 0 else { return nil }
        return rate
    }
    var ratesAddress = property(kAudioDevicePropertyAvailableNominalSampleRates)
    var size: UInt32 = 0
    guard let original = currentRate(),
          AudioObjectGetPropertyDataSize(device.id, &ratesAddress, 0, nil, &size) == noErr else { return .unavailable }
    let stride = MemoryLayout<AudioValueRange>.stride
    guard Int(size) >= stride else { return .unavailable }
    var ranges = [AudioValueRange](repeating: AudioValueRange(), count: Int(size) / stride)
    guard ranges.withUnsafeMutableBytes({ bytes in
        var count = UInt32(bytes.count)
        return AudioObjectGetPropertyData(device.id, &ratesAddress, 0, nil, &count, bytes.baseAddress!) == noErr
    }), let alternate = alternateMicrophoneRate(from: original, supported: ranges),
        allowActiveDevice || runningOnDevice() == 0 else { return .unavailable }

    return cycleMicrophoneClock(original: original, alternate: alternate, set: { target in
        var rate = target
        return AudioObjectSetPropertyData(device.id, &nominal, 0, nil, UInt32(MemoryLayout<Double>.size), &rate) == noErr
    }, settled: { target in
        for _ in 0..<10 {
            if let rate = currentRate(), abs(rate - target) <= 0.5 { return true }
            Thread.sleep(forTimeInterval: 0.01)
        }
        return false
    }, prime: {
        do {
            let temporary = try openMicrophone(device) { _, _ in }
            temporary.inputNode.removeTap(onBus: 0)
            temporary.stop()
            return true
        } catch {
            return false
        }
    })
}

enum WarmMicrophoneRecovery: String {
    case reopen, renegotiate
}

struct MeetingMicrophoneSilence {
    enum Recovery { case quick, released }

    private var began: Double?
    private var end: Double?
    private var owners: Set<pid_t>?
    private var released = false
    private var quickReopens = 0
    private var releaseReopens = 0
    private var ignoreReleasesUntil = 0.0
    private var deferred: Recovery?
    private(set) var generation = 0
    private(set) var warning = false

    mutating func observe(from start: Double, to finish: Double, lastSignalEnd: Double?) {
        if let lastSignalEnd {
            warning = false
            began = lastSignalEnd
            released = false
            deferred = nil
            releaseReopens = 0
            generation &+= 1
        } else if began == nil || end.map({ abs(start - $0) > 0.002 }) == true {
            began = start
        }
        end = finish
    }

    func ready(at now: Double) -> Bool {
        guard let began, let end else { return false }
        return abs(now - end) < 0.35 && end - began >= 3
    }

    mutating func poll(at now: Double, callUsesInput: Bool, activeOwners: Set<pid_t>?) -> Recovery? {
        if let activeOwners {
            if now >= ignoreReleasesUntil, let owners, !owners.isSubset(of: activeOwners), end != nil {
                released = true
            }
            owners = activeOwners
        }
        guard ready(at: now) else { return nil }
        if let deferred { return deferred }
        if warning { return released && releaseReopens < 3 ? .released : nil }
        guard callUsesInput else { return nil }
        if quickReopens < 2 { return .quick }
        warning = true
        return nil
    }

    mutating func deferRecovery(_ recovery: Recovery) {
        deferred = recovery
        warning = true
    }

    mutating func reportFailedOpen() { warning = true }

    mutating func beginRecovery(_ recovery: Recovery, at now: Double) {
        switch recovery {
        case .quick: quickReopens += 1
        case .released:
            releaseReopens += 1
            released = false
        }
        deferred = nil
        began = nil
        end = nil
        ignoreReleasesUntil = now + 1.5
    }

    mutating func changeDevice() {
        began = nil
        end = nil
        owners = nil
        released = false
        deferred = nil
        releaseReopens = 0
        ignoreReleasesUntil = 0
        generation &+= 1
    }

    mutating func breakStream() {
        began = nil
        end = nil
        generation &+= 1
    }
}

struct MeetingInputRoute {
    private(set) var callUID: String?
    private var previousCandidates = [InputDevice]()
    private var failures = 0
    private var retryAt: Double? = 0

    mutating func observeCall(_ reported: [String]) {
        if let first = reported.first, callUID.map(reported.contains) != true { callUID = first }
    }

    func candidates(savedUID: String?, available: [InputDevice], system: InputDevice) -> [InputDevice] {
        var candidates = [callUID, savedUID].compactMap { uid in available.first { $0.uid == uid } }
        if system.id != 0 { candidates.append(system) }
        var seen = Set<AudioDeviceID>()
        return candidates.filter { seen.insert($0.id).inserted }
    }

    mutating func canOpen(_ candidates: [InputDevice], at now: Double, needsInput: Bool = false) -> Bool {
        if previousCandidates != candidates {
            previousCandidates = candidates
            resetRetry()
        }
        if needsInput && retryAt == nil { retryAt = now }
        return retryAt.map { now >= $0 } ?? false
    }

    mutating func resetRetry() {
        failures = 0
        retryAt = 0
    }

    mutating func retry(after now: Double, hasWorkingInput: Bool) {
        retryAt = failures < 8 || !hasWorkingInput ? now + min(30, Double(1 << min(failures, 5))) : nil
        failures = min(failures + 1, 8)
    }
}

private let zoomInputBundles = ["us.zoom.xos", "us.zoom.cpthost", "us.zoom.caphost", "us.zoom.aomhost",
                                "us.zoom.zoomcefhelper", "us.zoom.zccimeetinghost", "us.zoom.zoomphone", "us.zoom.zoomhybridconf"]
private let faceTimeInputBundles = ["com.apple.facetime", "com.apple.avconferenced", "com.apple.telephonyutilities.callservicesd"]
private let browserInputVariants = ["com.google.chrome.canary", "com.google.chrome.beta", "com.google.chrome.dev",
                                    "com.brave.browser.beta", "com.brave.browser.nightly"]

func meetingInputMatches(_ application: String, bundle: String, processName: String?) -> Bool {
    let identifier = bundle.lowercased()
    func matches(_ candidate: String) -> Bool { identifier == candidate || identifier.hasPrefix(candidate + ".") }
    if let variant = browserInputVariants.first(where: matches) { return application == variant }
    if matches(application) { return true }
    if zoomInputBundles.contains(application) { return zoomInputBundles.contains(where: matches) }
    if faceTimeInputBundles.contains(application) { return faceTimeInputBundles.contains(where: matches) }
    if application == "org.mozilla.firefox" && matches("org.mozilla.plugincontainer") { return true }
    if application == "app.zen-browser.zen" && matches("app.zen-browser.plugincontainer") { return true }
    if identifier == "com.apple.webkit.gpu", let name = processName?.lowercased(), name.hasPrefix("safari") {
        return application == (name.hasPrefix("safari technology preview") ? "com.apple.safaritechnologypreview" : "com.apple.safari")
    }
    return false
}

func microphoneUsers(_ application: String, device: AudioDeviceID) -> (callInputs: [String], owners: Set<pid_t>?)? {
    func read(_ selector: AudioObjectPropertySelector, of object: AudioObjectID, scope: AudioObjectPropertyScope,
              into data: UnsafeMutableRawPointer, bytes: inout UInt32) -> Bool {
        var address = AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
        return AudioObjectGetPropertyData(object, &address, 0, nil, &bytes, data) == noErr
    }
    func identifiers(_ selector: AudioObjectPropertySelector, of object: AudioObjectID,
                     scope: AudioObjectPropertyScope) -> [AudioObjectID]? {
        var address = AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
        var bytes: UInt32 = 0
        let width = MemoryLayout<AudioObjectID>.stride
        guard AudioObjectGetPropertyDataSize(object, &address, 0, nil, &bytes) == noErr,
              bytes <= 65_536, Int(bytes) % width == 0 else { return nil }
        if bytes == 0 { return [] }
        var result = [AudioObjectID](repeating: 0, count: Int(bytes) / width)
        let success = result.withUnsafeMutableBytes {
            read(selector, of: object, scope: scope, into: $0.baseAddress!, bytes: &bytes)
        }
        return success ? Array(result.prefix(Int(bytes) / width)) : nil
    }
    let global = kAudioObjectPropertyScopeGlobal
    func belongsToCall(_ pid: pid_t, object: AudioObjectID) -> Bool {
        var host = pid
        var processName: String?
        for _ in 0..<6 {
            guard host > 1 else { break }
            let running = NSRunningApplication(processIdentifier: host)
            if host == pid { processName = running?.localizedName }
            if let bundle = running?.bundleIdentifier, meetingInputMatches(application, bundle: bundle, processName: running?.localizedName) {
                return true
            }
            var process = proc_bsdinfo()
            let width = Int32(MemoryLayout.size(ofValue: process))
            guard proc_pidinfo(host, PROC_PIDTBSDINFO, 0, &process, width) == width else { break }
            let parent = pid_t(process.pbi_ppid)
            if parent == host { break }
            host = parent
        }
        var bundle: Unmanaged<CFString>?
        var bytes = UInt32(MemoryLayout.size(ofValue: bundle))
        guard read(kAudioProcessPropertyBundleID, of: object, scope: global, into: &bundle, bytes: &bytes),
              let reported = bundle?.takeRetainedValue() as String? else { return false }
        return meetingInputMatches(application, bundle: reported, processName: processName)
    }
    guard let processes = identifiers(kAudioHardwarePropertyProcessObjectList, of: AudioObjectID(kAudioObjectSystemObject),
                                      scope: global) else { return nil }
    var owners = Set<pid_t>()
    var complete = true
    var callInputs = [String]()
    for object in processes {
        var running: UInt32 = 0
        var bytes = UInt32(MemoryLayout.size(ofValue: running))
        guard read(kAudioProcessPropertyIsRunningInput, of: object, scope: global, into: &running, bytes: &bytes) else {
            complete = false
            continue
        }
        guard running != 0 else { continue }
        guard let devices = identifiers(kAudioProcessPropertyDevices, of: object, scope: kAudioObjectPropertyScopeInput) else {
            complete = false
            continue
        }
        var pid: pid_t = 0
        bytes = UInt32(MemoryLayout.size(ofValue: pid))
        guard read(kAudioProcessPropertyPID, of: object, scope: global, into: &pid, bytes: &bytes) else {
            complete = false
            continue
        }
        guard pid > 1, pid != getpid(), pid != getppid() else { continue }
        var process = proc_bsdinfo()
        let width = Int32(MemoryLayout.size(ofValue: process))
        if proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &process, width) == width, process.pbi_ppid == getppid() { continue }
        if devices.contains(device) { owners.insert(pid) }
        guard belongsToCall(pid, object: object) else { continue }
        for id in devices {
            if let uid = audioDeviceString(kAudioDevicePropertyDeviceUID, of: id), !callInputs.contains(uid) {
                callInputs.append(uid)
            }
        }
    }
    return (callInputs, complete ? owners : nil)
}

final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate {
    private struct MicrophoneInterruption {
        let kind: String
        let message: String
        let frame: Int
        let failedAt: Double
    }
    private struct MeetingMicRequest {
        let id: UUID
        let recovery: MeetingMicrophoneSilence.Recovery
        let generation: Int
    }
    static let microphoneStallSeconds = 4.0
    private static let maximumMicrophoneGap = Timeline.rate / 4
    let mode: CaptureMode
    let path: String
    private let queue = DispatchQueue(label: "jard.capture.mix")
    private let systemQueue = DispatchQueue(label: "jard.capture.system")
    private let micControl = DispatchQueue(label: "jard.capture.microphone")
    private let systemConverter = MonoConverter()
    private let stopReason: AsyncStream<String>.Continuation
    private var input: InputDevice
    private let inputUID: String?
    private let avoidBluetooth: Bool
    private let externalMicrophone: Bool
    private let sharedMeetingID: UUID?
    private let retainSources: Bool
    private let callApplication: String?
    private let detailedMeter: Bool
    private let recoverColdOpen: Bool
    private var micConverter: MonoConverter?
    private var engine: AVAudioEngine?
    private var systemStream: SCStream?
    private var meetingSystemTask: Task<Void, Never>?
    private var meetingSystemStopped: DispatchSemaphore?
    private let meetingSystemMode: MeetingSystemAudioMode
    private let meetingInputsChanged: (([String]) -> Void)?
    private let hostPID: pid_t?
    private var observer: NSObjectProtocol?
    // Accessed on micControl only. Retiring an old observer before installing its
    // replacement avoids notifications from a detached device restarting the new tap.
    private var micEnabled = true
    private var generation = 0
    private var retryAfter = 0.0
    private var meetingInputRoute = MeetingInputRoute()
    private var meetingSavedUID: String?
    private var timer: DispatchSourceTimer?
    // Only the mix queue accesses the fields below.
    private var sink: WaveSink?
    private var microphoneSink: WaveSink?
    private var systemSink: WaveSink?
    private var drainError: String?
    private var timeline: Timeline
    private var epoch = 0.0
    private var startFrame = 0
    private var microphoneInterruption: MicrophoneInterruption?
    private var accepting = false
    private var levels = [Source: Float]()
    private var meetingLevel = MeetingAudioLevel()
    private var micEnergy = 0.0
    private var micFrames = 0
    private var firstLiveSignalFrame: Int?
    private var warmRecovery: WarmMicrophoneRecovery?
    private var lastMicCallback = 0.0
    private var nextInputCheck = 0.0
    private var lastTick = 0.0
    private var voice = DictationVoice()
    private var pendingReleaseTime: Double?
    private var stopRequested = false
    private var usingDefaultFallback = false
    private var releaseFrame: Int?
    private var releaseCutoff: Int?
    private var meetingSilence = MeetingMicrophoneSilence()
    private var nextCallInputPoll = 0.0
    private var callInputPollInFlight = false
    private var meetingMicRequest: MeetingMicRequest?
    private var micObservationGeneration: Int?
    private var meetingHandoffFrame: Int?
    private var meetingSystemID: UUID?
    private var systemHandoffFrame: Int?

    init(mode: CaptureMode, path: String, stopReason: AsyncStream<String>.Continuation,
         inputUID: String? = nil, avoidBluetooth: Bool = false, externalMicrophone: Bool = false, retainSources: Bool = false,
         detailedMeter: Bool = false, recoverColdOpen: Bool = false, callApplication: String? = nil,
         meetingSystemMode: MeetingSystemAudioMode = .screen, hostPID: pid_t? = nil,
         sharedMeetingID: UUID? = nil, meetingInputsChanged: (([String]) -> Void)? = nil) {
        self.mode = mode
        self.path = path
        self.stopReason = stopReason
        self.inputUID = inputUID
        self.meetingSavedUID = inputUID
        self.avoidBluetooth = avoidBluetooth
        self.externalMicrophone = externalMicrophone
        self.sharedMeetingID = sharedMeetingID
        self.meetingInputsChanged = meetingInputsChanged
        self.meetingSystemMode = meetingSystemMode
        self.hostPID = hostPID
        self.retainSources = retainSources
        self.callApplication = retainSources ? callApplication?.lowercased() : nil
        self.detailedMeter = detailedMeter
        self.recoverColdOpen = recoverColdOpen
        self.input = mode == .system ? .unavailable : InputDevice.current()
        self.timeline = Timeline(mode: mode)
    }

    private func emitCapture(_ fields: [String: Any]) {
        if let sharedMeetingID {
            var event = fields
            event["sharedMeeting"] = sharedMeetingID.uuidString.lowercased()
            emit(event)
        } else {
            emit(fields)
        }
    }

    func start(sharedInput: InputDevice? = nil) async throws {
        if mode != .system {
            guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
                throw RecordingPermissionError(source: .mic)
            }
        }
        if mode != .mic && !retainSources {
            do { try await startSystem() }
            catch where isSystemCaptureDenial(error) {
                throw RecordingPermissionError(source: .system)
            }
        }
        if mode != .system && !externalMicrophone {
            do {
                if recoverColdOpen {
                    let result = try await openColdMicrophone(
                        selected: inputUID.map { _ in { self.microphoneRoute().saved ?? .unavailable } },
                        systemDefault: {
                            let route = self.microphoneRoute()
                            return try route.require(route.system)
                        },
                        stopped: { self.queue.sync { self.openingStopped } },
                        open: { device in
                            guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
                                throw RecordingPermissionError(source: .mic)
                            }
                            try self.micControl.sync { try self.startMic(device) }
                        }
                    )
                    if result == .abandoned { abandonColdOpen(); return }
                    queue.sync { usingDefaultFallback = inputUID != nil && result == .systemDefault }
                } else if retainSources {
                    micControl.sync {
                        if let callApplication, let usage = microphoneUsers(callApplication, device: 0) {
                            meetingInputRoute.observeCall(usage.callInputs)
                        }
                        do { try openMeetingMicrophone() }
                        catch { queue.sync { input = .unavailable } }
                    }
                } else if isDictationMicrophone {
                    let route = microphoneRoute()
                    try micControl.sync { try startMic(route.require(route.primary)) }
                    queue.sync { usingDefaultFallback = inputUID != nil && route.saved == nil }
                } else {
                    _ = try micControl.sync { try microphoneRoute().openRecordingInput(open: startMic) }
                }
            } catch let permission as RecordingPermissionError {
                throw permission
            } catch {
                if recoverColdOpen && queue.sync(execute: { openingStopped }) { abandonColdOpen(); return }
                throw MicrophoneOpenError(error.localizedDescription)
            }
        }
        try beginWriting(input: sharedInput)
        if retainSources { startMeetingSystemAudio() }
    }

    func startMeetingSystemAudio(open: @escaping MeetingSystemAudioOpener = openMeetingSystemAudio,
                                 openTimeout: Duration = .seconds(3)) {
        let stopped = DispatchSemaphore(value: 0)
        meetingSystemStopped = stopped
        meetingSystemTask = Task { [weak self, meetingSystemMode, hostPID] in
            defer { stopped.signal() }
            await superviseMeetingSystemAudio(mode: meetingSystemMode, hostPID: hostPID, receive: { id, samples, time in
                self?.enqueue(samples, at: time, from: .system, systemID: id)
            }, change: { available, backend in
                guard let self, self.isWriting else { return }
                var event: [String: Any] = ["event": "meetingSystemAudio", "available": available]
                if let backend { event["backend"] = backend.rawValue }
                self.emitCapture(event)
            }, permissionDenied: {
                guard let self, self.isWriting else { return }
                self.emitCapture(["event": "meetingSystemAudio", "available": false, "permissionDenied": true])
            }, open: open, openTimeout: openTimeout)
        }
    }

    func beginWriting(input external: InputDevice? = nil, usingDefault: Bool? = nil,
                      from start: Double? = nil, preRollLimit: Double = 0.45) throws {
        let event = try queue.sync { () throws -> [String: Any]? in
            if recoverColdOpen && openingStopped { return nil }
            sink = try WaveSink(path: path)
            if retainSources {
                microphoneSink = try WaveSink(path: path + ".mic.wav", create: false)
                systemSink = try WaveSink(path: path + ".system.wav", create: false)
            }
            if let external { input = external }
            if let usingDefault { usingDefaultFallback = usingDefault }
            let now = hostSeconds()
            epoch = start.map { max(now - preRollLimit, min(now, $0)) } ?? now
            startFrame = Int(((now - epoch) * Double(Timeline.rate)).rounded())
            lastMicCallback = now
            lastTick = now
            accepting = true
            if let pendingReleaseTime {
                self.pendingReleaseTime = nil
                beginReleaseTail(at: pendingReleaseTime)
            }
            var started: [String: Any] = ["event": "started", "mode": mode.rawValue, "microphone": input.name,
                                         "openedAt": now, "usingDefaultMicrophone": usingDefaultFallback]
            if retainSources { started["microphoneAvailable"] = input.id != 0 }
            return started
        }
        guard let event else { abandonColdOpen(); return }
        emitCapture(event)
        let clock = DispatchSource.makeTimerSource(queue: queue)
        let meterInterval = detailedMeter ? 0.05 : 0.25
        clock.schedule(deadline: .now() + meterInterval, repeating: meterInterval, leeway: .milliseconds(detailedMeter ? 5 : 15))
        clock.setEventHandler { [weak self] in self?.tick() }
        timer = clock
        clock.resume()
    }

    private var isDictationMicrophone: Bool { mode == .mic && detailedMeter }

    private func microphoneRoute() -> DictationInputRoute {
        DictationInputRoute.current(savedUID: inputUID, avoidBluetooth: avoidBluetooth)
    }

    private func startMic(_ selected: InputDevice) throws {
        guard selected.id != 0 else { throw CaptureError("Choose an input in System Settings → Sound.") }
        let converter = MonoConverter()
        let installed = generation + 1
        let instance = try openMicrophone(selected, pinDevice: retainSources || isDictationMicrophone || selected.uid == inputUID) { [weak self] buffer, time in
            guard let self else { return }
            do {
                let timestamp = time.isHostTimeValid ? AVAudioTime.seconds(forHostTime: time.hostTime)
                    : hostSeconds() - Double(buffer.frameLength) / buffer.format.sampleRate
                if self.callApplication != nil { self.observeMeetingMicrophone(buffer, at: timestamp, generation: installed) }
                try converter.append(buffer, at: timestamp) {
                    self.enqueue($0, at: $1, from: .mic, microphoneGeneration: self.retainSources ? installed : nil)
                }
            } catch {
                if self.retainSources { self.requestMicRestart(generation: installed, stalled: true) }
                else { self.microphoneFailed(error.localizedDescription) }
            }
        }
        // Other routes can fail to open. Leave the old tap attached until the new one starts.
        if engine != nil { detachMic() }
        micConverter = converter
        engine = instance
        queue.sync {
            if input.id != selected.id || input.uid != selected.uid {
                meetingSilence.changeDevice()
                nextCallInputPoll = 0
            }
            input = selected
            micObservationGeneration = installed
            if retainSources && accepting { meetingHandoffFrame = max(timeline.micEnd, timeline.cursor) }
            lastMicCallback = hostSeconds()
        }
        generation = installed
        observer = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange,
            object: instance, queue: nil) { [weak self] _ in
            self?.requestMicRestart(generation: installed)
        }
    }

    private func detachMic() {
        queue.sync {
            micObservationGeneration = nil
            meetingSilence.breakStream()
        }
        if let observer { NotificationCenter.default.removeObserver(observer) }
        observer = nil
        guard let engine else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        finishConverter(micConverter, from: .mic, recoverable: retainSources)
        micConverter = nil
        self.engine = nil
    }

    private func observeMeetingMicrophone(_ buffer: AVAudioPCMBuffer, at start: Double, generation: Int) {
        guard let channels = buffer.floatChannelData, start.isFinite, buffer.frameLength > 0 else { return }
        let rate = buffer.format.sampleRate
        let count = Int(buffer.frameLength)
        let lastSignal = (0..<Int(buffer.format.channelCount)).reduce(-1) { last, channel in
            let samples = channels[channel]
            return ((last + 1)..<count).last { samples[$0 * buffer.stride] != 0 } ?? last
        }
        let signalEnd = lastSignal < 0 ? nil : start + Double(lastSignal + 1) / rate
        let finish = start + Double(count) / rate
        guard abs(finish - hostSeconds()) < 10 else { return }
        queue.async {
            guard self.accepting, self.micObservationGeneration == generation else { return }
            self.updateMeetingWarning { $0.observe(from: start, to: finish, lastSignalEnd: signalEnd) }
        }
    }

    private func updateMeetingWarning(_ change: (inout MeetingMicrophoneSilence) -> Void) {
        let previous = meetingSilence.warning
        change(&meetingSilence)
        if meetingSilence.warning != previous {
            emitCapture(["event": "meetingMicWarning", "active": meetingSilence.warning])
        }
    }

    func selectMeetingInput(_ uid: String?) {
        guard retainSources && !externalMicrophone else { return }
        micControl.async {
            guard self.meetingSavedUID != uid else { return }
            self.meetingSavedUID = uid
            self.meetingInputRoute.resetRetry()
            #if !JARD_CAPTURE_FIXTURE
            self.refreshMeetingMicrophone()
            #endif
        }
    }

    func sharedMeetingInput(_ device: InputDevice, generation: Int?, unavailable: String? = nil) {
        guard sharedMeetingID != nil else { return }
        queue.async {
            guard self.accepting else { return }
            let previous = self.input
            self.input = device
            if self.micObservationGeneration != generation {
                self.micObservationGeneration = generation
                self.meetingHandoffFrame = max(self.timeline.micEnd, self.timeline.cursor)
                self.updateMeetingWarning {
                    if previous != device { $0.changeDevice() }
                    else { $0.breakStream() }
                }
            }
            if previous != device {
                self.emitCapture(["event": "sourceChanged", "microphone": device.name,
                    "microphoneAvailable": device.id != 0,
                    "message": unavailable ?? "Recording from " + device.name])
            }
        }
    }

    // The recording queue owns the displayed route and the capture event order.
    private func publishMeetingMicrophone(_ device: InputDevice, message: String) {
        queue.sync {
            guard accepting, !stopRequested else { return }
            if device.id == 0 {
                guard input.id != 0 else { return }
                input = device
            }
            emitCapture(["event": "sourceChanged", "microphone": device.name,
                         "microphoneAvailable": device.id != 0, "message": message])
        }
    }

    // The control queue owns the route, and each candidate can fail independently.
    private func openMeetingMicrophone(reopenCurrent: Bool = false) throws {
        let now = hostSeconds()
        let lidClosed = MicrophoneLid.closed
        let candidates = meetingInputRoute.candidates(savedUID: meetingSavedUID, available: InputDevice.available(),
                                                     system: InputDevice.current())
            .filter { !(lidClosed && $0.isInternalMicrophone) }
        let previous = queue.sync { input }
        if engine != nil && (reopenCurrent || (lidClosed && previous.isInternalMicrophone)) { detachMic() }
        guard meetingInputRoute.canOpen(candidates, at: now, needsInput: engine == nil) else {
            if engine == nil { throw CaptureError("Waiting for the meeting microphone.") }
            return
        }
        var failure: Error?
        for candidate in candidates {
            if candidate.id == previous.id && candidate.uid == previous.uid && engine != nil { break }
            do {
                try startMic(candidate)
                if failure != nil { meetingInputRoute.retry(after: now, hasWorkingInput: true) }
                else { meetingInputRoute.resetRetry() }
                publishMeetingMicrophone(candidate, message: "Recording from " + candidate.name)
                return
            } catch {
                failure = error
            }
        }
        if failure != nil || engine == nil { meetingInputRoute.retry(after: now, hasWorkingInput: engine != nil) }
        else { meetingInputRoute.resetRetry() }
        if engine == nil { throw failure ?? CaptureError("Choose an input in System Settings → Sound.") }
    }

    private func refreshMeetingMicrophone(reopenCurrent: Bool = false) {
        let active = queue.sync { accepting && !stopRequested }
        guard micEnabled, active else { return }
        do { try openMeetingMicrophone(reopenCurrent: reopenCurrent) }
        catch {
            publishMeetingMicrophone(.unavailable, message: error.localizedDescription)
        }
    }

    private func pollMeetingInput(at now: Double) {
        guard retainSources, now >= nextCallInputPoll, !callInputPollInFlight else { return }
        nextCallInputPoll = now + 1
        callInputPollInFlight = true
        let previous = input
        micControl.async {
            let usage = self.callApplication.flatMap { microphoneUsers($0, device: previous.id) }
            if !self.externalMicrophone {
                if let usage { self.meetingInputRoute.observeCall(usage.callInputs) }
                let stalled = self.queue.sync { hostSeconds() - self.lastMicCallback > 1 }
                self.refreshMeetingMicrophone(reopenCurrent: stalled)
            } else if let usage {
                self.meetingInputsChanged?(usage.callInputs)
            }
            self.queue.async {
                self.callInputPollInFlight = false
                guard self.accepting, !self.stopRequested, self.input.id == previous.id, self.input.uid == previous.uid,
                      let usage else { return }
                var recovery: MeetingMicrophoneSilence.Recovery?
                self.updateMeetingWarning {
                    recovery = $0.poll(at: hostSeconds(), callUsesInput: usage.callInputs.contains(previous.uid),
                                       activeOwners: usage.owners)
                }
                if self.meetingMicRequest == nil, let recovery {
                    let id = UUID()
                    self.meetingMicRequest = MeetingMicRequest(id: id, recovery: recovery, generation: self.meetingSilence.generation)
                    self.emitCapture(["event": "meetingMicRecoveryRequest", "id": id.uuidString.lowercased()])
                }
            }
        }
    }

    func respondToMeetingMicRecovery(_ id: UUID, allowed: Bool) {
        micControl.async {
            defer {
                if allowed { self.emitCapture(["event": "meetingMicRecoveryDone", "id": id.uuidString.lowercased()]) }
            }
            guard self.micEnabled, let selected = self.claimMeetingMicRecovery(id, allowed: allowed) else { return }
            self.detachMic()
            let result = resetMicrophoneClock(selected, allowActiveDevice: true)
            self.emitCapture(["event": "microphoneClockReset", "result": result.rawValue])
            self.reopenMicrophone(selected, afterSilenceRecovery: true)
        }
    }

    func claimMeetingMicRecovery(_ id: UUID, allowed: Bool) -> InputDevice? {
        queue.sync {
            guard let request = meetingMicRequest, request.id == id else { return nil }
            meetingMicRequest = nil
            let now = hostSeconds()
            guard accepting, !stopRequested, meetingSilence.generation == request.generation,
                  meetingSilence.ready(at: now) else { return nil }
            guard allowed else {
                updateMeetingWarning { $0.deferRecovery(request.recovery) }
                return nil
            }
            meetingSilence.beginRecovery(request.recovery, at: now)
            lastMicCallback = now
            retryAfter = now + 0.75
            return input
        }
    }

    func reportMeetingMicRecoveryFailure() {
        queue.async { self.updateMeetingWarning { $0.reportFailedOpen() } }
    }

    private func reopenMicrophone(_ selected: InputDevice, afterSilenceRecovery: Bool = false) {
        do {
            try startMic(selected)
            if retainSources { publishMeetingMicrophone(selected, message: "Recording from " + selected.name) }
            else { emitCapture(["event": "sourceChanged", "microphone": selected.name, "message": "Recording from " + selected.name]) }
        } catch {
            detachMic()
            if afterSilenceRecovery {
                queue.sync { updateMeetingWarning { $0.reportFailedOpen() } }
            }
            if retainSources { publishMeetingMicrophone(.unavailable, message: error.localizedDescription) }
            else { emitCapture(["event": "sourceChanged", "microphone": "Unavailable", "message": error.localizedDescription]) }
        }
    }

    private func finishConverter(_ converter: MonoConverter?, from source: Source, recoverable: Bool = false) {
        do { try converter?.finish { enqueue($0, at: $1, from: source) } }
        catch {
            if recoverable { return }
            let message = error.localizedDescription
            queue.async { self.drainError = message }
            stopReason.yield(message)
        }
    }

    private func requestMicRestart(generation expected: Int? = nil, stalled: Bool = false) {
        micControl.async {
            guard self.micEnabled, expected == nil || expected == self.generation else { return }
            if self.retainSources {
                let now = hostSeconds()
                guard now >= self.retryAfter else { return }
                self.retryAfter = now + 0.75
                self.refreshMeetingMicrophone(reopenCurrent: stalled || self.engine?.isRunning != true)
                return
            }
            let (active, previous) = self.queue.sync { (self.accepting && !self.stopRequested, self.input) }
            guard active else { return }
            if self.isDictationMicrophone {
                let connected = previous.currentConnection()
                if (connected ?? previous).isInternalMicrophone && MicrophoneLid.closed {
                    self.microphoneFailed(MicrophoneLid.unavailableMessage)
                } else if connected == nil {
                    self.microphoneFailed("The microphone disconnected during dictation.")
                } else if self.engine?.isRunning != true {
                    self.microphoneFailed("The microphone stopped during dictation.")
                }
                return
            }
            let route = self.microphoneRoute()
            let keep = expected == nil && !stalled && self.engine?.isRunning == true ? previous : nil
            guard keep != route.primary else { return }
            let now = hostSeconds()
            guard now >= self.retryAfter else { return }
            self.retryAfter = now + 0.75
            if keep == nil { self.detachMic() }
            do {
                let selected = try route.openRecordingInput(keeping: keep, open: self.startMic)
                if selected != keep {
                    self.emitCapture(["event": "sourceChanged", "microphone": selected.name, "message": "Recording from " + selected.name])
                }
            } catch {
                if keep == nil {
                    self.queue.sync { self.input = .unavailable }
                    self.emitCapture(["event": "sourceChanged", "microphone": "Unavailable", "message": error.localizedDescription])
                }
            }
        }
    }

    private func startSystem() async throws {
        systemStream = try await openScreenSystemAudio(delegate: self, output: self, queue: systemQueue)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        stopReason.yield("Mac audio capture stopped. " + error.localizedDescription)
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sample: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio else { return }
        do {
            try withScreenAudio(sample) { pcm, time in
                try systemConverter.append(pcm, at: time) { enqueue($0, at: $1, from: .system) }
            }
        } catch { stopReason.yield(error.localizedDescription) }
    }

    var isWriting: Bool { queue.sync { accepting } }
    var warmMicrophoneRecovery: WarmMicrophoneRecovery? { queue.sync { warmRecovery } }

    #if JARD_CAPTURE_FIXTURE
    func simulateMicrophoneHandoff(to generation: Int) {
        queue.sync {
            micObservationGeneration = generation
            meetingHandoffFrame = max(timeline.micEnd, timeline.cursor)
        }
    }
    #endif

    func microphoneFailed(_ message: String, stalled: Bool = false) {
        if mode == .mic && detailedMeter {
            let failedAt = stalled ? nil : hostSeconds()
            queue.async { self.interruptMicrophone(message, failedAt: failedAt) }
        } else {
            stopReason.yield(message)
        }
    }

    private func interruptMicrophone(_ message: String, kind: String = "microphone", failedAt: Double? = nil) {
        guard accepting, microphoneInterruption == nil, releaseCutoff == nil else { return }
        let lastSample = epoch + Double(timeline.micEnd) / Double(Timeline.rate)
        microphoneInterruption = MicrophoneInterruption(kind: kind, message: message, frame: timeline.micEnd,
                                                       failedAt: failedAt ?? lastSample)
        stopReason.yield(message)
    }

    private let slots = DispatchSemaphore(value: 128)
    func enqueue(_ samples: [Float], at seconds: Double, from source: Source, microphoneGeneration: Int? = nil,
                 systemID: UUID? = nil) {
        guard !samples.isEmpty else { return }
        guard slots.wait(timeout: .now()) == .success else {
            stopReason.yield("Audio storage fell behind. Recording stopped to preserve the completed audio.")
            return
        }
        queue.async {
            defer { self.slots.signal() }
            guard self.accepting, self.microphoneInterruption == nil else { return }
            if let microphoneGeneration, self.micObservationGeneration != microphoneGeneration { return }
            guard seconds.isFinite, abs(seconds - hostSeconds()) < 10 else {
                self.stopReason.yield("Audio input lost its timing reference.")
                return
            }
            var frame = Int(((seconds - self.epoch) * Double(Timeline.rate)).rounded())
            var samples = samples
            var handoff = microphoneGeneration == nil ? nil : self.meetingHandoffFrame
            if let systemID {
                if systemID != self.meetingSystemID {
                    self.meetingSystemID = systemID
                    self.systemHandoffFrame = max(self.timeline.systemEnd, self.timeline.cursor)
                }
                handoff = self.systemHandoffFrame
            }
            if let handoff {
                let trim = max(0, handoff - frame)
                guard trim < samples.count else { return }
                if trim > 0 { samples = Array(samples.dropFirst(trim)); frame += trim }
                if source == .mic { self.meetingHandoffFrame = nil }
                else { self.systemHandoffFrame = nil }
            }
            if source == .mic {
                let previous = self.timeline.micEnd
                if self.detailedMeter && previous > 0 && frame - previous > Self.maximumMicrophoneGap {
                    self.interruptMicrophone("The microphone audio had a gap. Check the input and try again.", kind: "discontinuity")
                    return
                }
                self.lastMicCallback = hostSeconds()
                if self.sharedMeetingID != nil {
                    let to = self.epoch + Double(frame + samples.count) / Double(Timeline.rate)
                    let from = self.epoch + Double(frame) / Double(Timeline.rate)
                    let signal = samples.lastIndex { $0 != 0 }.map { from + Double($0 + 1) / Double(Timeline.rate) }
                    self.updateMeetingWarning { $0.observe(from: from, to: to, lastSignalEnd: signal) }
                }
                if self.detailedMeter {
                    self.micEnergy += samples.reduce(0.0) { $0 + Double($1 * $1) }
                    self.micFrames += samples.count
                    if frame < (self.firstLiveSignalFrame ?? Int.max),
                       let signal = samples.dropFirst(max(0, self.startFrame - frame)).firstIndex(where: { $0 != 0 }) {
                        let signalFrame = frame + signal
                        self.firstLiveSignalFrame = min(self.firstLiveSignalFrame ?? signalFrame, signalFrame)
                    }
                }
            }
            self.timeline.add(samples, at: frame, from: source)
            if source == .mic && self.detailedMeter {
                self.voice.observe(self.timeline)
                self.checkReleaseTail()
            }
            self.levels[source] = max(self.levels[source] ?? 0, samples.reduce(0) { max($0, abs($1)) })
            if self.retainSources {
                let end = self.epoch + Double(frame + samples.count) / Double(Timeline.rate)
                self.meetingLevel.observe(samples, from: source, endingAt: end)
            }
        }
    }

    func requestFinish(releasedAt: Double) {
        queue.async {
            guard self.mode == .mic, self.detailedMeter, self.releaseFrame == nil, self.pendingReleaseTime == nil else { return }
            if self.accepting {
                self.beginReleaseTail(at: releasedAt)
            } else if self.sink == nil {
                // The bridge can relay a shortcut finish before the device finishes opening.
                self.pendingReleaseTime = releasedAt
            }
        }
    }

    // Both a shortcut release and an immediate stop can arrive before open returns.
    private var openingStopped: Bool { stopRequested || pendingReleaseTime != nil }

    func requestStop(_ reason: String = "Stopped") {
        queue.async {
            self.stopRequested = true
            self.stopReason.yield(reason)
        }
    }

    private func abandonColdOpen() {
        emitCapture(["event": "openingAbandoned"])
        stopReason.yield("Stopped")
    }

    private func beginReleaseTail(at releasedAt: Double) {
        let now = hostSeconds()
        if releasedAt < epoch {
            if externalMicrophone { emitCapture(["event": "openingAbandoned"]) }
            releaseFrame = 0
            releaseCutoff = 0
            stopReason.yield("Stopped")
            return
        }
        let release = max(epoch, min(now, releasedAt))
        releaseFrame = Int(((release - epoch) * Double(Timeline.rate)).rounded())
        checkReleaseTail()
        // Let delayed device callbacks supply the chosen tail, with a bounded wait.
        queue.asyncAfter(deadline: .now() + max(0, release + 0.7 - now)) { [weak self] in
            self?.checkReleaseTail(deadline: true)
        }
    }

    private func checkReleaseTail(deadline: Bool = false) {
        guard accepting, microphoneInterruption == nil, let releaseFrame, releaseCutoff == nil else { return }
        let captured = timeline.micEnd
        let cutoff = voice.cutoff(after: releaseFrame, through: captured)
        guard cutoff != nil || deadline else { return }
        releaseCutoff = cutoff ?? min(captured, releaseFrame + DictationVoice.cap)
        stopReason.yield("Stopped")
    }

    // A one-second margin accommodates different mic and system callback latencies.
    private func tick() {
        guard accepting, microphoneInterruption == nil else { return }
        let now = hostSeconds()
        let elapsed = now - epoch
        if now - lastTick > 5 {
            stopReason.yield("Recording was interrupted while the Mac or the recorder was paused.")
            return
        }
        lastTick = now
        #if !JARD_CAPTURE_FIXTURE
        pollMeetingInput(at: now)
        #endif
        if mode == .mic && detailedMeter && now - lastMicCallback > Self.microphoneStallSeconds {
            interruptMicrophone("The microphone stopped delivering audio. Check the input before recording again.")
            return
        } else if mode != .system && !retainSources && now - lastMicCallback > 5 {
            stopReason.yield("The microphone stopped delivering audio. Check the input before recording again.")
        }
        if elapsed >= 3 * 3600 { stopReason.yield("Three-hour recording limit reached.") }
        #if !JARD_CAPTURE_FIXTURE
        if mode != .system && !retainSources && !externalMicrophone && now >= nextInputCheck {
            nextInputCheck = now + 1
            requestMicRestart(stalled: now - lastMicCallback > 1)
        }
        #endif
        let commit = Int((elapsed - 1) * Double(Timeline.rate))
        do { try flush(to: detailedMeter ? min(commit, timeline.micEnd) : commit) }
        catch { stopReason.yield("Could not write the recording. " + error.localizedDescription) }
        var meter: [String: Any] = ["event": "levels", "mic": min(1, levels[.mic] ?? 0), "system": min(1, levels[.system] ?? 0),
                                   "seconds": max(0, elapsed), "microphone": input.name]
        if detailedMeter {
            meter["micRms"] = micFrames > 0 ? sqrt(micEnergy / Double(micFrames)) : 0
            micEnergy = 0
            micFrames = 0
        }
        if retainSources { meter["meetingRms"] = meetingLevel.rms(at: now) }
        emitCapture(meter)
        levels.removeAll(keepingCapacity: true)
    }

    private func flush(to frame: Int) throws {
        let end = min(frame, Timeline.limit)
        while timeline.cursor < end {
            let chunk = timeline.renderWindow(through: min(end, timeline.cursor + Timeline.rate / 4),
                                              includeSources: retainSources)
            try sink?.append(chunk.mixed)
            try microphoneSink?.append(chunk.microphone)
            try systemSink?.append(chunk.system)
        }
    }

    func markMeetingCopy(_ request: UUID) {
        let pressed = hostSeconds()
        queue.sync {
            guard retainSources, accepting else { return }
            // Identify the cut on the capture clock. The regular writer will commit it
            // after the margin for callbacks from the two independent audio sources.
            let elapsed = max(0, pressed - epoch)
            let frame = min(Timeline.limit, Int(elapsed * Double(Timeline.rate)))
            emitCapture(["event": "meetingCopy", "request": request.uuidString.lowercased(), "frame": frame])
        }
    }

    func finish(reason: String) async throws {
        let stopped = hostSeconds()
        timer?.cancel()
        micControl.sync {
            micEnabled = false
            detachMic()
        }
        meetingSystemTask?.cancel()
        meetingSystemTask = nil
        if let stopped = meetingSystemStopped {
            meetingSystemStopped = nil
            // A synchronous Core Audio open may be waiting for the system.
            // Drain ordinary stops briefly, then finish the tracks on our clock.
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                DispatchQueue.global(qos: .userInitiated).async {
                    _ = stopped.wait(timeout: .now() + .milliseconds(500))
                    continuation.resume()
                }
            }
        }
        if let systemStream {
            try? await systemStream.stopCapture()
            systemQueue.sync { finishConverter(systemConverter, from: .system) }
        }
        try queue.sync {
            guard accepting, let sink else { return }
            accepting = false
            // A suspend/clock jump must not expand a truncated take into hours of silence.
            let stoppedFrame = Int(((stopped - epoch) * Double(Timeline.rate)).rounded())
            let capturedEnd = detailedMeter ? min(stoppedFrame, timeline.micEnd) : stoppedFrame
            let interruption = microphoneInterruption
            let end = min(releaseCutoff ?? interruption?.frame ?? capturedEnd, timeline.cursor + 3 * Timeline.rate)
            var finalReason = drainError ?? interruption?.message ?? reason
            var wrote = drainError == nil
            do { try flush(to: end) }
            catch {
                wrote = false
                finalReason = "Recording stopped after a storage error. " + error.localizedDescription
            }
            if externalMicrophone && detailedMeter {
                let liveEnd = min(end, timeline.micEnd, releaseFrame ?? end)
                if interruption != nil {
                    warmRecovery = .reopen
                } else if wrote && reason == "Stopped" && liveEnd > startFrame && (firstLiveSignalFrame ?? liveEnd) >= liveEnd {
                    warmRecovery = .renegotiate
                }
            }
            try sink.finish()
            try microphoneSink?.finish()
            try systemSink?.finish()
            var event: [String: Any] = ["event": "finished", "reason": finalReason, "path": path,
                                        "seconds": Double(sink.frames) / Double(Timeline.rate)]
            if timeline.lateFrames > 0 { event["lateFrames"] = timeline.lateFrames }
            if let releaseFrame { event["releaseFrame"] = min(releaseFrame, sink.frames) }
            if wrote, let interruption {
                event["interruption"] = interruption.kind
                event["failedAt"] = interruption.failedAt
                event["capturedThrough"] = epoch + Double(sink.frames) / Double(Timeline.rate)
                event["hasLiveAudio"] = sink.frames > startFrame
            }
            emitCapture(event)
        }
    }
}

struct MicrophoneOpenError: LocalizedError {
    let message: String
    let failedAt = hostSeconds()
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}
