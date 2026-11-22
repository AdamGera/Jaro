import AVFoundation
import CoreAudio
import Foundation

struct WarmMicrophoneRetry {
    private static let initialDelay = 0.25
    private var delay = initialDelay
    private var backingOffStream = false
    private var retryAt = 0.0

    func canOpen(at now: Double, retryNow: Bool = false) -> Bool {
        retryNow || now >= retryAt
    }

    mutating func openFailed(at now: Double) {
        retryAt = now + delay
        delay = min(delay * 2, backingOffStream ? 60 : 5)
    }

    mutating func opened() {
        retryAt = 0
        if !backingOffStream { delay = Self.initialDelay }
    }

    mutating func deliveredSteadily() {
        backingOffStream = false
        delay = Self.initialDelay
    }

    mutating func streamFailed(at now: Double, steady: Bool) {
        if steady {
            deliveredSteadily()
            retryAt = now
        } else {
            backingOffStream = true
            openFailed(at: now)
        }
    }
}

struct WarmMicrophoneDelivery {
    var openedAt: Double
    private var firstPacketAt: Double?
    private var lastPacketAt: Double?
    var failed = false

    init(openedAt: Double) { self.openedAt = openedAt }

    var steady: Bool {
        guard let firstPacketAt, let lastPacketAt else { return false }
        return lastPacketAt - firstPacketAt >= 2 * Recorder.microphoneStallSeconds
    }

    func stalled(at now: Double) -> Bool {
        failed || now - (lastPacketAt ?? openedAt) >= Recorder.microphoneStallSeconds
    }

    mutating func receive(at now: Double) -> Bool {
        if failed || (lastPacketAt != nil && stalled(at: now)) {
            failed = true
            return false
        }
        firstPacketAt = firstPacketAt ?? now
        lastPacketAt = now
        return true
    }
}

private struct WarmInputOpen {
    let generation: Int
    let route: DictationInputRoute
    let device: InputDevice
    let preferred: Bool
    let onDemand: Bool
}

private struct MeetingInputOpen {
    let meeting: UUID
    let generation: Int
    let candidates: [InputDevice]
    let recovery: UUID?
}

private final class WarmInputStream {
    private(set) var engine: AVAudioEngine?

    init(_ engine: AVAudioEngine?) { self.engine = engine }

    func close() {
        guard let engine else { return }
        self.engine = nil
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
    }

    deinit { close() }
}

private struct WarmInputStatus: Equatable {
    let phase: String
    let microphone: String
    let message: String
}

/// Own the mic while idle and keep a short in-memory window for capture handoffs.
/// Each accepted stage still uses the ordinary timed WAV recorder.
private final class WarmMicrophone {
    private static let preRollDuration = 0.45
    private static let handoffWindow = 1.5
    private var uid: String?
    private let avoidBluetooth: Bool
    private let lock = NSLock()
    private let events: AsyncStream<WarmEvent>.Continuation
    private let opener = DispatchQueue(label: "jaro.capture.warm-open", qos: .userInitiated)
    private var stream: WarmInputStream?
    private var opening: WarmInputOpen?
    private var meetingOpening: MeetingInputOpen?
    private var meetingIdentifier: UUID?
    private var meetingSavedUID: String?
    private var meetingInputRoute = MeetingInputRoute()
    private var annotationHandoffUID: String?
    private var device: InputDevice = .unavailable
    private var recent: [MicrophonePacket] = []
    private var standbyStartedAt = 0.0
    private var take: Recorder?
    private var previousRoute: DictationInputRoute?
    private var savedRetry = WarmMicrophoneRetry()
    private var retry = WarmMicrophoneRetry()
    private var initialOpenAttempts = 0
    private var initialOpenRetryAt = 0.0
    private var delivery: WarmMicrophoneDelivery?
    private var nextStreamGeneration = 0
    private var streamGeneration: Int?
    private var clockResetUID: String?
    private var availability: WarmInputStatus?
    private var meterUntil = 0.0
    private var meterReading: (generation: Int, at: Double, peak: Float)?
    private var annotationNarration: AnnotationNarration?
    private var sharedMeeting: Recorder?
    #if JARD_CAPTURE_FIXTURE
    private var fixtureOpenFailures = ProcessInfo.processInfo.environment["JARD_TEST_WARM_OPEN_FAILURES"].flatMap(Int.init)
    private let fixtureSavedInputGate = ProcessInfo.processInfo.environment["JARD_TEST_WARM_SAVED_INPUT_GATE"]
    private let fixtureTracksLid = ProcessInfo.processInfo.environment["JARD_TEST_MICROPHONE_LID_GATE"] != nil
    private let fixtureOpenGate = ProcessInfo.processInfo.environment["JARD_TEST_WARM_OPEN_GATE"]
    private let fixtureGatedUID = ProcessInfo.processInfo.environment["JARD_TEST_WARM_GATED_UID"]
    private let fixtureCallInput = ProcessInfo.processInfo.environment["JARD_TEST_SHARED_MEETING_CALL_INPUT"]
    #endif

    init(uid: String?, avoidBluetooth: Bool, events: AsyncStream<WarmEvent>.Continuation) {
        self.uid = uid
        self.avoidBluetooth = avoidBluetooth
        self.events = events
    }

    private func locked<T>(_ operation: () throws -> T) rethrows -> T {
        lock.lock()
        defer { lock.unlock() }
        return try operation()
    }

    private func currentRoute() -> DictationInputRoute {
        let uid = annotationHandoffUID ?? self.uid
        #if JARD_CAPTURE_FIXTURE
        let system = InputDevice(id: 1, name: "Fixture system microphone", uid: "fixture",
                                 transport: kAudioDeviceTransportTypeBuiltIn)
        var available = [system]
        if let uid, uid != system.uid {
            available.append(InputDevice(id: 2, name: "Fixture saved microphone", uid: uid, transport: kAudioDeviceTransportTypeUSB))
        }
        if fixtureTracksLid {
            available.append(InputDevice(id: 3, name: "Fixture USB microphone", uid: "fixture-usb", transport: kAudioDeviceTransportTypeUSB))
        }
        return DictationInputRoute.resolve(savedUID: uid, available: available, systemDefault: system,
                                           lidClosed: MicrophoneLid.closed, avoidBluetooth: avoidBluetooth)
        #else
        return DictationInputRoute.current(savedUID: uid, avoidBluetooth: avoidBluetooth)
        #endif
    }

    func prepare(retryNow: Bool, firstDemand: Bool) throws -> Bool {
        #if !JARD_CAPTURE_FIXTURE
        guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
            closeEngine()
            let error = RecordingPermissionError(source: .mic)
            publishAvailability("unavailable", message: error.localizedDescription)
            throw error
        }
        #endif
        if let meetingIdentifier { return prepareMeetingInput(meetingIdentifier) }
        if meetingOpening != nil { return usableInput() }
        let route = currentRoute()
        #if JARD_CAPTURE_FIXTURE
        let running = true
        #else
        let running = stream?.engine?.isRunning == true
        #endif
        let changedRoute = previousRoute != route
        if previousRoute?.saved != route.saved {
            savedRetry = WarmMicrophoneRetry()
            initialOpenAttempts = 0
            initialOpenRetryAt = 0
        }
        previousRoute = route
        let now = hostSeconds()
        var workingInput = false
        if let observed = locked({ delivery }) {
            let healthy = running && !observed.stalled(at: now)
            if observed.steady { retry.deliveredSteadily() }
            if healthy && device == route.primary {
                publishInput(for: route)
                return true
            }
            if healthy, let connected = connectedInput() {
                workingInput = (connected == route.system || connected.canStandInForSystemInput) &&
                    !(connected.isInternalMicrophone && MicrophoneLid.closed)
            }
            if workingInput {
                publishInput(for: route)
            } else {
                closeEngine()
                if !healthy {
                    retry.streamFailed(at: now, steady: observed.steady)
                    publishAvailability("unavailable", message: "Jaro is reconnecting the microphone.")
                }
            }
        }

        let immediate = changedRoute || retryNow
        if opening != nil || (workingInput && retryNow) { return workingInput }
        if firstDemand {
            if workingInput { return true }
            guard now >= initialOpenRetryAt else { return false }
            if let saved = route.saved, initialOpenAttempts < 3 {
                initialOpenAttempts += 1
                open(saved, route: route, preferred: true, onDemand: true)
            } else {
                do {
                    open(try route.require(route.system), route: route, preferred: false, onDemand: true)
                } catch {
                    throw MicrophoneOpenError(error.localizedDescription)
                }
            }
            return false
        }
        guard retry.canOpen(at: now, retryNow: immediate) else { return workingInput }
        if route.hasFallback, let saved = route.saved,
           savedRetry.canOpen(at: now, retryNow: immediate) {
            open(saved, route: route, preferred: true)
        } else {
            let target = route.hasFallback ? route.system : route.primary
            if !workingInput || (target.id != 0 && device != target) {
                do {
                    open(try route.require(target), route: route, preferred: false)
                } catch {
                    retry.openFailed(at: now)
                    publishAvailability("unavailable", message: error.localizedDescription)
                    throw error
                }
            }
        }
        return workingInput
    }

    private func usableInput() -> Bool {
        #if !JARD_CAPTURE_FIXTURE
        guard stream?.engine?.isRunning == true else { return false }
        #endif
        guard let connected = connectedInput(), !(connected.isInternalMicrophone && MicrophoneLid.closed) else { return false }
        return locked { delivery.map { !$0.stalled(at: hostSeconds()) } ?? false }
    }

    private func currentMeetingCandidates() -> [InputDevice] {
        #if JARD_CAPTURE_FIXTURE
        let system = InputDevice(id: 1, name: "Fixture system microphone", uid: "fixture", transport: kAudioDeviceTransportTypeBuiltIn)
        var available = [system]
        if let meetingSavedUID, meetingSavedUID != system.uid {
            available.append(InputDevice(id: 2, name: "Fixture saved microphone", uid: meetingSavedUID,
                                         transport: kAudioDeviceTransportTypeUSB))
        }
        if let callUID = meetingInputRoute.callUID, !available.contains(where: { $0.uid == callUID }) {
            available.append(InputDevice(id: 3, name: "Fixture call microphone", uid: callUID, transport: kAudioDeviceTransportTypeUSB))
        }
        #else
        let system = InputDevice.current()
        let available = InputDevice.available()
        #endif
        let lidClosed = MicrophoneLid.closed
        return meetingInputRoute.candidates(savedUID: meetingSavedUID, available: available, system: system)
            .filter { !(lidClosed && $0.isInternalMicrophone) }
    }

    private func prepareMeetingInput(_ identifier: UUID) -> Bool {
        #if JARD_CAPTURE_FIXTURE
        if let fixtureCallInput, let input = try? String(contentsOfFile: fixtureCallInput, encoding: .utf8), !input.isEmpty {
            meetingInputRoute.observeCall([input])
        }
        #endif
        let working = usableInput()
        if !working && stream != nil {
            closeEngine()
            publishAvailability("unavailable", message: "Jaro is reconnecting the meeting microphone.")
        }
        guard opening == nil, meetingOpening == nil else { return working }
        let candidates = currentMeetingCandidates()
        guard meetingInputRoute.canOpen(candidates, at: hostSeconds(), needsInput: !working) else { return working }
        let replacements = working ? Array(candidates.prefix { $0 != device }) : candidates
        if replacements.isEmpty {
            if working { meetingInputRoute.resetRetry() }
            else {
                meetingInputRoute.retry(after: hostSeconds(), hasWorkingInput: false)
                publishAvailability("unavailable", message: "Choose an input in System Settings → Sound.")
            }
        } else {
            openMeetingInput(identifier, candidates: candidates, devices: replacements)
        }
        return working
    }

    private func openMeetingInput(_ id: UUID, candidates: [InputDevice], devices: [InputDevice], recovery: UUID? = nil) {
        nextStreamGeneration += 1
        let request = MeetingInputOpen(meeting: id, generation: nextStreamGeneration, candidates: candidates, recovery: recovery)
        meetingOpening = request
        opener.async { [weak self, events] in
            guard let self else { return }
            var failure: Error = CaptureError("The meeting microphone is unavailable.")
            for selected in devices {
                do {
                    if recovery != nil { try self.resetClock(for: selected, allowActiveDevice: true) }
                    let replacement = WarmInputStream(try self.openInput(selected, generation: request.generation))
                    events.yield(.meetingInputOpened(request, .success((selected, replacement))))
                    return
                } catch {
                    failure = error
                    if error is RecordingPermissionError { break }
                }
            }
            events.yield(.meetingInputOpened(request, .failure(failure)))
        }
    }

    func acceptMeetingInput(_ request: MeetingInputOpen, result: Result<(InputDevice, WarmInputStream), Error>, canSwitch: Bool) {
        defer { if let recovery = request.recovery { emitMeetingRecoveryDone(recovery, meeting: request.meeting) } }
        let isCurrent = meetingOpening?.generation == request.generation
        if isCurrent { meetingOpening = nil }
        guard isCurrent, canSwitch, request.meeting == meetingIdentifier,
              request.recovery != nil || request.candidates == currentMeetingCandidates() else {
            if case let .success((_, replacement)) = result { retire(replacement) }
            return
        }
        switch result {
        case let .success((selected, replacement)):
            installStream(replacement, device: selected, generation: request.generation)
            if selected == request.candidates.first { meetingInputRoute.resetRetry() }
            else { meetingInputRoute.retry(after: hostSeconds(), hasWorkingInput: true) }
            publishInput(for: currentRoute())
        case let .failure(error):
            if request.recovery != nil { locked { sharedMeeting?.reportMeetingMicRecoveryFailure() } }
            let working = usableInput()
            meetingInputRoute.retry(after: hostSeconds(), hasWorkingInput: working)
            if !working { publishAvailability("unavailable", message: error.localizedDescription) }
        }
    }

    private func installStream(_ replacement: WarmInputStream, device selected: InputDevice, generation: Int) {
        let previous = stream
        stream = replacement
        device = selected
        locked {
            streamGeneration = generation
            delivery = WarmMicrophoneDelivery(openedAt: hostSeconds())
            standbyStartedAt = 0
            recent.removeAll()
            sharedMeeting?.sharedMeetingInput(selected, generation: generation)
        }
        retire(previous)
        clockResetUID = nil
    }

    private func open(_ selected: InputDevice, route: DictationInputRoute, preferred: Bool, onDemand: Bool = false) {
        nextStreamGeneration += 1
        let request = WarmInputOpen(generation: nextStreamGeneration, route: route, device: selected,
                                    preferred: preferred, onDemand: onDemand)
        opening = request
        let resetClock = !selected.uid.isEmpty && clockResetUID == selected.uid
        if resetClock { clockResetUID = nil }
        opener.async { [weak self, events] in
            guard let self else { return }
            let result = Result {
                if resetClock { try self.resetClock(for: selected) }
                return WarmInputStream(try self.openInput(selected, generation: request.generation))
            }
            events.yield(.opened(request, result))
        }
    }

    func acceptOpen(_ request: WarmInputOpen, result: Result<WarmInputStream, Error>, canSwitch: Bool) throws {
        let isCurrent = opening?.generation == request.generation
        if isCurrent { opening = nil }
        guard isCurrent && meetingIdentifier == nil && request.route == currentRoute() else {
            if case let .success(replacement) = result { retire(replacement) }
            return
        }
        switch result {
        case let .success(replacement):
            guard canSwitch else {
                retire(replacement)
                return
            }
            installStream(replacement, device: request.device, generation: request.generation)
            retry.opened()
            if request.preferred { savedRetry.opened() }
            publishInput(for: request.route)
        case let .failure(error):
            let now = hostSeconds()
            if error is RecordingPermissionError {
                if canSwitch {
                    closeEngine()
                    publishAvailability("unavailable", message: error.localizedDescription)
                }
                throw error
            }
            if request.onDemand {
                guard request.preferred else { throw MicrophoneOpenError(error.localizedDescription) }
                initialOpenRetryAt = initialOpenAttempts < 3 ? now + 0.1 : now
                if initialOpenAttempts < 3 {
                    opener.asyncAfter(deadline: .now() + .milliseconds(100)) { [events] in
                        events.yield(.maintenance)
                    }
                }
                return
            }
            if request.preferred {
                savedRetry.openFailed(at: now)
            } else {
                retry.openFailed(at: now)
                if stream == nil { publishAvailability("unavailable", message: error.localizedDescription) }
                throw error
            }
        }
    }

    private func publishInput(for route: DictationInputRoute) {
        let fallback = uid.map { $0 != device.uid } ?? (device != route.system)
        publishAvailability(fallback ? "fallback" : "ready", microphone: device.name)
    }

    private func publishAvailability(_ phase: String, microphone: String = "", message: String = "") {
        let status = WarmInputStatus(phase: phase, microphone: microphone, message: message)
        guard status != availability else { return }
        availability = status
        if phase == "unavailable" {
            locked { sharedMeeting?.sharedMeetingInput(.unavailable, generation: nil, unavailable: message) }
        }
        emit(["event": "warmAvailability", "phase": phase, "microphone": microphone, "message": message])
    }

    private func resetClock(for selected: InputDevice, allowActiveDevice: Bool = false) throws {
        #if JARD_CAPTURE_FIXTURE
        emit(["event": "fixtureWarmClockReset"])
        #else
        let reset = resetMicrophoneClock(selected, allowActiveDevice: allowActiveDevice)
        emit(["event": "microphoneClockReset", "result": reset.rawValue])
        if reset == .restoreFailed { throw CaptureError("Could not restore the microphone’s sample rate.") }
        #endif
    }

    private func openInput(_ selected: InputDevice, generation: Int) throws -> AVAudioEngine? {
        #if JARD_CAPTURE_FIXTURE
        if let failures = fixtureOpenFailures {
            emit(["event": "fixtureWarmOpen", "failing": failures > 0, "time": hostSeconds()])
            if failures > 0 {
                fixtureOpenFailures = failures - 1
                throw CaptureError("Fixture microphone could not open.")
            }
        }
        if fixtureSavedInputGate != nil || fixtureTracksLid || fixtureCallInput != nil {
            let failing = fixtureSavedInputGate.map { selected.uid == uid && !FileManager.default.fileExists(atPath: $0) } ?? false
            emit(["event": "fixtureWarmRouteOpen", "uid": selected.uid, "failing": failing, "time": hostSeconds()])
            if failing { throw CaptureError("Fixture saved microphone could not open.") }
        }
        if let gate = fixtureOpenGate, fixtureGatedUID == nil || fixtureGatedUID == selected.uid,
           !FileManager.default.fileExists(atPath: gate) {
            emit(["event": "fixtureWarmOpening", "uid": selected.uid])
            let deadline = hostSeconds() + 10
            while !FileManager.default.fileExists(atPath: gate) && hostSeconds() < deadline {
                Thread.sleep(forTimeInterval: 0.02)
            }
            guard FileManager.default.fileExists(atPath: gate) else { throw CaptureError("Fixture open timed out.") }
        }
        return nil
        #else
        guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
            throw RecordingPermissionError(source: .mic)
        }
        let converter = MonoConverter()
        return try openMicrophone(selected, pinDevice: true) { [weak self] buffer, time in
            guard let self else { return }
            let timestamp = time.isHostTimeValid ? AVAudioTime.seconds(forHostTime: time.hostTime)
                : hostSeconds() - Double(buffer.frameLength) / buffer.format.sampleRate
            do {
                try converter.append(buffer, at: timestamp) { self.receive($0, time: $1, generation: generation) }
            } catch {
                self.locked {
                    guard self.streamGeneration == generation else { return }
                    self.delivery?.failed = true
                    self.take?.microphoneFailed(error.localizedDescription)
                }
            }
        }
        #endif
    }

    private func receive(_ samples: [Float], time: Double, generation: Int) {
        locked {
            guard generation == streamGeneration else { return }
            let now = hostSeconds()
            if delivery?.receive(at: now) == false {
                take?.microphoneFailed("The microphone stopped delivering audio.", stalled: true)
                return
            }
            if now < meterUntil && !samples.isEmpty {
                let mean = samples.reduce(0, +) / Float(samples.count)
                let peak = samples.reduce(Float(0)) { max($0, abs($1 - mean)) }
                meterReading = (generation, now, min(1, peak))
            }
            recent.append((samples, time))
            let cutoff = time - Self.handoffWindow
            recent.removeAll { $0.time < cutoff }
            // Also bound memory if the device's clock is interrupted.
            let overflow = recent.count - 256
            if overflow > 0 { recent.removeFirst(overflow) }
            take?.enqueue(samples, at: time, from: .mic)
            annotationNarration?.enqueue(samples, at: time)
            sharedMeeting?.enqueue(samples, at: time, from: .mic, microphoneGeneration: generation)
        }
    }

    func beginAnnotation(path: String, at start: Double) throws {
        let narration = try AnnotationNarration(path: path, start: start)
        try locked {
            guard annotationNarration == nil else { throw CaptureError("Annotation narration is already recording.") }
            for packet in microphonePreRoll(recent, from: start) {
                narration.enqueue(packet.samples, at: packet.time)
            }
            annotationNarration = narration
        }
    }

    func finishAnnotation(at end: Double?) async throws {
        annotationHandoffUID = nil
        let narration = locked {
            let previous = annotationNarration
            annotationNarration = nil
            return previous
        }
        try await narration?.finish(at: end)
    }

    func beginMeeting(_ request: WarmMeeting, path: String, stop: AsyncStream<String>.Continuation) async throws {
        let recorder = Recorder(mode: .both, path: path, stopReason: stop, externalMicrophone: true, retainSources: true,
                                callApplication: request.callApplication, meetingSystemMode: request.systemMode,
                                hostPID: request.hostPID, sharedMeetingID: request.meeting, meetingInputsChanged: { [events] inputs in
                                    events.yield(.meetingInputs(request.meeting, inputs))
                                })
        #if JARD_CAPTURE_FIXTURE
        try startSyntheticInput(recorder, input: device, includeMicrophone: false)
        if ProcessInfo.processInfo.environment["JARD_TEST_STALLED_MEETING_SYSTEM_AUDIO"] == "1" {
            recorder.startMeetingSystemAudio(open: { _, _, _ in
                await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                    DispatchQueue.global().asyncAfter(deadline: .now() + 6) { continuation.resume() }
                }
                throw CaptureError("Fixture Mac audio is unavailable.")
            })
        }
        #else
        try await recorder.start(sharedInput: stream != nil ? device : .unavailable)
        #endif
        meetingIdentifier = request.meeting
        meetingSavedUID = request.inputDevice ?? uid
        meetingInputRoute = MeetingInputRoute()
        #if !JARD_CAPTURE_FIXTURE
        if let application = request.callApplication, let usage = microphoneUsers(application.lowercased(), device: device.id) {
            meetingInputRoute.observeCall(usage.callInputs)
        }
        #endif
        locked {
            recorder.sharedMeetingInput(streamGeneration == nil ? .unavailable : device, generation: streamGeneration)
            sharedMeeting = recorder
        }
    }

    func observeMeetingInputs(_ id: UUID, inputs: [String]) {
        guard id == meetingIdentifier else { return }
        meetingInputRoute.observeCall(inputs)
    }

    func respondToMeetingRecovery(_ request: UUID, meeting: UUID, allowed: Bool) {
        let canReset = allowed && opening == nil && meetingOpening == nil
        let recorder = locked { sharedMeeting }
        guard let selected = recorder?.claimMeetingMicRecovery(request, allowed: canReset), selected.id != 0 else {
            if allowed { emitMeetingRecoveryDone(request, meeting: meeting) }
            return
        }
        closeEngine()
        openMeetingInput(meeting, candidates: currentMeetingCandidates(), devices: [selected], recovery: request)
    }

    private func emitMeetingRecoveryDone(_ recovery: UUID, meeting: UUID) {
        emit(["event": "meetingMicRecoveryDone", "id": recovery.uuidString.lowercased(),
              "sharedMeeting": meeting.uuidString.lowercased()])
    }

    func copyMeeting(_ request: UUID) {
        let recorder = locked { sharedMeeting }
        recorder?.markMeetingCopy(request)
    }

    func selectSharedInput(_ input: String?) {
        guard input != meetingSavedUID else { return }
        meetingSavedUID = input
        meetingInputRoute.resetRetry()
    }

    func detachMeeting() -> Recorder? {
        meetingIdentifier = nil
        meetingSavedUID = nil
        return locked {
            let previous = sharedMeeting
            sharedMeeting = nil
            if annotationNarration != nil && streamGeneration != nil { annotationHandoffUID = device.uid }
            return previous
        }
    }

    func meterPeak() -> Float? {
        locked {
            let now = hostSeconds()
            meterUntil = now + 0.65
            guard let meterReading, meterReading.generation == streamGeneration,
                  now - meterReading.at <= 0.25 else { return nil }
            return meterReading.peak
        }
    }

    private func connectedInput() -> InputDevice? {
        #if JARD_CAPTURE_FIXTURE
        return device
        #else
        return device.currentConnection()
        #endif
    }

    func checkActiveDevice() {
        let connected = connectedInput()
        if (connected ?? device).isInternalMicrophone && MicrophoneLid.closed {
            locked { take?.microphoneFailed(MicrophoneLid.unavailableMessage) }
        } else if connected == nil {
            locked { take?.microphoneFailed("The microphone disconnected during dictation.") }
        }
    }

    func begin(path: String, preRoll: Bool, queuedAt: Double?, stop: AsyncStream<String>.Continuation) throws {
        try locked {
            guard take == nil else { throw CaptureError("A warm take is already recording.") }
            let recorder = Recorder(mode: .mic, path: path, stopReason: stop, externalMicrophone: true, detailedMeter: true)
            #if JARD_CAPTURE_FIXTURE
            if recent.isEmpty { recent = [(Array(repeating: -0.25, count: 960), hostSeconds() - 0.12)] }
            #endif
            let now = hostSeconds()
            let queued = queuedAt.flatMap { $0.isFinite && $0 > 0 ? min($0, now) : nil }
            let limit = queued == nil ? Self.preRollDuration : Self.handoffWindow
            let boundary = if let queued {
                max(now - limit, queued - Self.preRollDuration)
            } else {
                max(now - limit, standbyStartedAt)
            }
            let fresh = preRoll ? microphonePreRoll(recent, from: boundary) : []
            let usingFallback = uid.map { $0 != device.uid } ?? false
            #if JARD_CAPTURE_FIXTURE
            guard let generation = streamGeneration else { throw CaptureError("Fixture stream is unavailable.") }
            try startSyntheticInput(recorder, input: device, usingDefault: usingFallback, preRoll: fresh, preRollLimit: limit) { [weak self] samples, time in
                self?.receive(samples, time: time, generation: generation)
            }
            #else
            try recorder.beginWriting(input: device, usingDefault: usingFallback,
                                      from: fresh.first?.time, preRollLimit: limit)
            for packet in fresh { recorder.enqueue(packet.samples, at: packet.time, from: .mic) }
            #endif
            take = recorder
        }
    }

    func finish(reason: String) async throws {
        let recorder = locked {
            let recorder = take
            take = nil
            standbyStartedAt = hostSeconds()
            return recorder
        }
        if let recorder {
            defer {
                if let recovery = recorder.warmMicrophoneRecovery,
                   recovery == .reopen || !locked({ annotationNarration != nil || sharedMeeting != nil }) {
                    let now = hostSeconds()
                    if recovery == .renegotiate { clockResetUID = device.uid }
                    let steady = recovery == .renegotiate || locked { delivery?.steady == true }
                    closeEngine()
                    retry.streamFailed(at: now, steady: steady)
                    if recovery == .reopen {
                        publishAvailability("unavailable", message: "Jaro is reconnecting the microphone.")
                    }
                    #if JARD_CAPTURE_FIXTURE
                    emit(["event": "fixtureWarmReset", "reason": recovery.rawValue])
                    #endif
                }
            }
            try await recorder.finish(reason: reason)
        }
    }

    func requestFinish(releasedAt: Double) {
        locked { take?.requestFinish(releasedAt: releasedAt) }
    }

    func closeEngine() {
        locked {
            streamGeneration = nil
            delivery = nil
            standbyStartedAt = 0
            recent.removeAll()
        }
        let previous = stream
        stream = nil
        retire(previous)
    }

    private func retire(_ retired: WarmInputStream?) {
        guard let retired else { return }
        opener.async { retired.close() }
    }

    deinit { closeEngine() }
}

private enum WarmEvent {
    case command(String)
    case ended(Int, String)
    case meetingEnded(UUID, String)
    case meetingInputs(UUID, [String])
    case meetingInputOpened(MeetingInputOpen, Result<(InputDevice, WarmInputStream), Error>)
    case opened(WarmInputOpen, Result<WarmInputStream, Error>)
    case maintenance
    case eof
}

private struct WarmStart: Decodable {
    let start: String
    let preRoll: Bool?
    let queuedAt: Double?
}

private struct WarmMeter: Decodable {
    let meter: Int
}

private struct WarmAnnotation: Decodable {
    enum Action: String, Decodable { case start, stop, cancel }
    let annotation: Int
    let action: Action
    let path: String?
    let at: Double?
}

private struct WarmMeeting: Decodable {
    let meeting: UUID
    let start: String?
    let systemAudio: String?
    let hostPID: Int32?
    let callApplication: String?
    let inputDevice: String?
    let command: String?

    var systemMode: MeetingSystemAudioMode { systemAudio.flatMap(MeetingSystemAudioMode.init(rawValue:)) ?? .screen }
}

func runWarmMicrophone(uid: String?, avoidBluetooth: Bool, onDemand: Bool) async {
    #if !JARD_CAPTURE_FIXTURE
    guard onDemand || AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
        emit(["event": "error", "message": RecordingPermissionError(source: .mic).localizedDescription])
        return
    }
    #endif
    let (events, queue) = AsyncStream<WarmEvent>.makeStream()
    let mic = WarmMicrophone(uid: uid, avoidBluetooth: avoidBluetooth, events: queue)
    emit(["event": "warmReady"])
    DispatchQueue.global().async {
        while let line = readLine() { queue.yield(.command(line)) }
        queue.yield(.eof)
    }
    let maintenance = Task {
        while !Task.isCancelled {
            do { try await Task.sleep(for: .milliseconds(250)) } catch { return }
            queue.yield(.maintenance)
        }
    }
    defer {
        queue.finish()
        maintenance.cancel()
    }
    var generation = 0
    var stop: AsyncStream<String>.Continuation?
    var pending: WarmStart?
    var startedInput = !onDemand
    var meeting: (id: UUID, stop: AsyncStream<String>.Continuation)?
    var finishingMeeting: Task<Void, Never>?

    func reportMeetingError(_ id: UUID, _ error: Error) {
        let key = id.uuidString.lowercased()
        var event: [String: Any] = ["event": "error", "sharedMeeting": key, "message": error.localizedDescription]
        if let permission = error as? RecordingPermissionError { event["permission"] = permission.source.rawValue }
        emit(event)
        emit(["event": "takeEnded", "sharedMeeting": key])
    }

    func failPending(_ error: Error) {
        guard pending != nil else { return }
        pending = nil
        var event: [String: Any] = ["event": "error", "message": error.localizedDescription]
        if let permission = error as? RecordingPermissionError { event["permission"] = permission.source.rawValue }
        if let microphone = error as? MicrophoneOpenError {
            event["interruption"] = "microphone"
            event["failedAt"] = microphone.failedAt
        }
        emit(event)
        emit(["event": "takeEnded"])
    }

    func abandonPending() {
        guard pending != nil else { return }
        pending = nil
        emit(["event": "openingAbandoned"])
        emit(["event": "takeEnded"])
    }

    func refreshInput(retryNow: Bool = false) {
        guard startedInput || pending != nil else { return }
        do {
            let ready = try mic.prepare(retryNow: retryNow, firstDemand: !startedInput)
            guard ready, let start = pending else { return }
            let (reasons, continuation) = AsyncStream<String>.makeStream(bufferingPolicy: .bufferingOldest(1))
            try mic.begin(path: start.start, preRoll: startedInput && (start.preRoll ?? true), queuedAt: start.queuedAt, stop: continuation)
            startedInput = true
            pending = nil
            stop = continuation
            generation += 1
            let id = generation
            Task {
                var iterator = reasons.makeAsyncIterator()
                if let reason = await iterator.next() { queue.yield(.ended(id, reason)) }
            }
        } catch {
            failPending(error)
        }
    }

    refreshInput()
    for await event in events {
        switch event {
        case let .command(line):
            if let release = dictationReleaseTime(line) {
                if stop == nil { abandonPending() }
                else { mic.requestFinish(releasedAt: release) }
            } else if line == "stop" || line == "cancel" {
                if stop == nil { abandonPending() }
                else { stop?.yield(line == "cancel" ? "Cancelled" : "Stopped") }
            } else if let data = line.data(using: .utf8), let request = try? JSONDecoder().decode(WarmMeter.self, from: data) {
                emit(["event": "warmMeter", "request": request.meter, "peak": mic.meterPeak().map { $0 as Any } ?? NSNull()])
            } else if let data = line.data(using: .utf8), let request = try? JSONDecoder().decode(WarmAnnotation.self, from: data) {
                do {
                    let now = hostSeconds()
                    switch request.action {
                    case .start:
                        guard let path = request.path, path.hasPrefix("/"), let start = request.at,
                              start.isFinite, start > 0, start <= now + 0.1, now - start <= 5 else {
                            throw CaptureError("The annotation audio start time is unavailable.")
                        }
                        try mic.beginAnnotation(path: path, at: start)
                    case .stop:
                        guard let end = request.at, end.isFinite && end > 0 && end <= now + 0.1 else {
                            throw CaptureError("The annotation audio stop time is unavailable.")
                        }
                        // Wait briefly for callbacks through the video stop boundary.
                        try await Task.sleep(for: .milliseconds(100))
                        try await mic.finishAnnotation(at: end)
                    case .cancel:
                        try await mic.finishAnnotation(at: nil)
                    }
                    emit(["event": "warmAnnotation", "request": request.annotation])
                } catch {
                    emit(["event": "warmAnnotation", "request": request.annotation, "error": error.localizedDescription])
                }
            } else if let data = line.data(using: .utf8), let request = try? JSONDecoder().decode(WarmMeeting.self, from: data) {
                if let path = request.start, path.hasPrefix("/"), meeting == nil {
                    let (reasons, continuation) = AsyncStream<String>.makeStream(bufferingPolicy: .bufferingOldest(1))
                    do {
                        try await mic.beginMeeting(request, path: path, stop: continuation)
                        meeting = (request.meeting, continuation)
                        startedInput = true
                        if stop == nil { refreshInput(retryNow: true) }
                        Task {
                            var iterator = reasons.makeAsyncIterator()
                            if let reason = await iterator.next() { queue.yield(.meetingEnded(request.meeting, reason)) }
                        }
                    } catch {
                        continuation.finish()
                        reportMeetingError(request.meeting, error)
                    }
                } else if meeting?.id == request.meeting, let command = request.command {
                    if command == "stop" { meeting?.stop.yield("Stopped") }
                    else if command.hasPrefix("copy "), let id = UUID(uuidString: String(command.dropFirst(5))) {
                        mic.copyMeeting(id)
                    } else if command.hasPrefix("input "),
                              let input = try? JSONDecoder().decode(String.self, from: Data(command.dropFirst(6).utf8)),
                              !input.contains("\0") {
                        mic.selectSharedInput(input.isEmpty ? nil : input)
                        if stop == nil { refreshInput(retryNow: true) }
                    } else {
                        let fields = command.split(separator: " ")
                        if fields.count == 2, fields[0] == "recover-mic" || fields[0] == "defer-mic",
                           let id = UUID(uuidString: String(fields[1])) {
                            mic.respondToMeetingRecovery(id, meeting: request.meeting, allowed: fields[0] == "recover-mic")
                        }
                    }
                }
            } else if stop == nil, pending == nil, let data = line.data(using: .utf8),
                      let start = try? JSONDecoder().decode(WarmStart.self, from: data), start.start.hasPrefix("/") {
                pending = start
                refreshInput(retryNow: true)
            }
        case let .opened(request, result):
            do {
                try mic.acceptOpen(request, result: result, canSwitch: stop == nil && (startedInput || pending != nil))
                if stop == nil { refreshInput() }
            } catch {
                failPending(error)
            }
        case let .meetingInputs(id, inputs):
            mic.observeMeetingInputs(id, inputs: inputs)
            if stop == nil { refreshInput() }
        case let .meetingInputOpened(request, result):
            mic.acceptMeetingInput(request, result: result, canSwitch: stop == nil)
            if stop == nil { refreshInput() }
        case let .ended(id, reason):
            guard id == generation, let active = stop else { continue }
            active.finish()
            stop = nil
            do { try await mic.finish(reason: reason) }
            catch { emit(["event": "error", "message": error.localizedDescription]) }
            emit(["event": "takeEnded"])
        case let .meetingEnded(id, reason):
            guard let active = meeting, active.id == id else { continue }
            active.stop.finish()
            meeting = nil
            let recorder = mic.detachMeeting()
            finishingMeeting = Task {
                do {
                    try await recorder?.finish(reason: reason)
                    emit(["event": "takeEnded", "sharedMeeting": id.uuidString.lowercased()])
                } catch {
                    reportMeetingError(id, error)
                }
            }
        case .maintenance:
            if stop == nil { refreshInput() }
            else { mic.checkActiveDevice() }
        case .eof:
            stop?.finish()
            meeting?.stop.finish()
            try? await mic.finish(reason: "Recorder connection closed")
            try? await mic.finishAnnotation(at: nil)
            try? await mic.detachMeeting()?.finish(reason: "Recorder connection closed")
            await finishingMeeting?.value
            mic.closeEngine()
            return
        }
    }
}
