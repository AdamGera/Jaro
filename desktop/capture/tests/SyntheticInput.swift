// Compiled only in the process-lifecycle tests. Never opens a capture device.
import CoreMedia
import Foundation

private struct SyntheticSystemFallback: MeetingSystemAudioConnection {
    let openedAt = hostSeconds()
    let packets: Task<Void, Never>?
    var onStop: (() -> Void)?

    var readiness: MeetingSystemAudioReadiness {
        if packets != nil { return .ready }
        return hostSeconds() - openedAt < 0.4 ? .waiting : .failed
    }

    func stop() async {
        packets?.cancel()
        await packets?.value
        onStop?()
    }
}

func startSyntheticInput(_ recorder: Recorder, input: InputDevice? = nil, usingDefault: Bool? = nil,
                         preRoll: [MicrophonePacket] = [], preRollLimit: Double = 0.45, includeMicrophone: Bool = true,
                         receive: (([Float], Double) -> Void)? = nil) throws {
    if let name = ProcessInfo.processInfo.environment["JARD_TEST_DENIED_SOURCE"], let source = Source(rawValue: name) {
        throw RecordingPermissionError(source: source)
    }
    if ProcessInfo.processInfo.environment["JARD_TEST_START_DELAY"] == "1" {
        Thread.sleep(forTimeInterval: 0.25)
    }
    if ProcessInfo.processInfo.environment["JARD_TEST_OPEN_FAILURE"] == "1" {
        throw MicrophoneOpenError("The fixture microphone could not open.")
    }
    try recorder.beginWriting(input: input, usingDefault: usingDefault, from: preRoll.first?.time, preRollLimit: preRollLimit)
    for packet in preRoll { recorder.enqueue(packet.samples, at: packet.time, from: .mic) }
    let start = CMClockGetTime(CMClockGetHostTimeClock()).seconds
    let fail = ProcessInfo.processInfo.environment["JARD_TEST_INPUT_LOSS"] == "1"
    let handoff = ProcessInfo.processInfo.environment["JARD_TEST_MIC_HANDOFF"] == "1"
    let systemRestart = ProcessInfo.processInfo.environment["JARD_TEST_SYSTEM_RESTART"] == "1"
    let fallback = ProcessInfo.processInfo.environment["JARD_TEST_SYSTEM_FALLBACK"]
    let initialSystem = UUID(), restartedSystem = UUID()
    let interruption = ProcessInfo.processInfo.environment["JARD_TEST_INTERRUPTION"]
    let speechEnd = ProcessInfo.processInfo.environment["JARD_TEST_SPEECH_FRAMES"].flatMap(Int.init)
    let speechStart = ProcessInfo.processInfo.environment["JARD_TEST_FIRST_SPEECH_FRAME"].flatMap(Int.init) ?? 8
    let speech = (0..<960).map { Float(sin(Double($0) * 0.08) * 0.1) }
    let quiet = [Float](repeating: 0, count: 960)
    let constant = [Float](repeating: 0.25, count: 960)
    if let fallback {
        var firstTap = true
        recorder.startMeetingSystemAudio(open: { backend, _, forward in
            if backend == .tap {
                let recovering = fallback == "recover"
                if fallback == "open" || (recovering && firstTap) {
                    firstTap = false
                    let unexpected = [Float](repeating: 0.75, count: 960)
                    forward(unexpected, hostSeconds())
                    try? await Task.sleep(for: .milliseconds(recovering ? 1700 : 850))
                    forward(unexpected, hostSeconds())
                    return SyntheticSystemFallback(packets: nil, onStop: { emit(["event": "fixtureLateTapClosed"]) })
                }
                if !recovering { return SyntheticSystemFallback(packets: nil) }
            }
            let packets = Task {
                let samples = [Float](repeating: 0.5, count: 960)
                var timestamp = hostSeconds()
                while !Task.isCancelled {
                    forward(samples, timestamp)
                    timestamp += 0.02
                    try? await Task.sleep(for: .seconds(max(0, timestamp - hostSeconds())))
                }
            }
            return SyntheticSystemFallback(packets: packets)
        }, openTimeout: .milliseconds(400))
    }
    Task.detached {
        var i = 0
        while !Task.isCancelled && recorder.isWriting {
            if interruption == "preRoll" || (i == 20 && interruption == "loss") {
                recorder.microphoneFailed("The fixture microphone disconnected.")
                return
            }
            if i == 20 && interruption == "stall" { return }
            let gap = i >= 20 && interruption == "gap" ? 0.28 : 0
            let timestamp = start + Double(i) * 0.02 + gap
            if recorder.mode != .system && includeMicrophone {
                let samples = speechEnd.map { (speechStart..<max(speechStart, $0)).contains(i) ? speech : quiet } ?? constant
                if handoff {
                    if i == 0 { recorder.simulateMicrophoneHandoff(to: 1) }
                    if i == 20 {
                        recorder.simulateMicrophoneHandoff(to: 2)
                        recorder.enqueue(constant, at: timestamp, from: .mic, microphoneGeneration: 1)
                        // The new tap's first packet reaches behind the old tap's final sample.
                        recorder.enqueue([Float](repeating: 0.125, count: 1_440), at: timestamp - 0.01,
                                         from: .mic, microphoneGeneration: 2)
                    } else {
                        recorder.enqueue(i < 20 ? constant : [Float](repeating: 0.125, count: 960),
                                         at: timestamp, from: .mic, microphoneGeneration: i < 20 ? 1 : 2)
                    }
                } else if let receive { receive(samples, timestamp) }
                else { recorder.enqueue(samples, at: timestamp, from: .mic) }
            }
            if recorder.mode != .mic && fallback == nil {
                if systemRestart {
                    if i < 20 || i >= 25 {
                        let systemLevel: Float = i < 20 ? 0.5 : 0.375
                        recorder.enqueue([Float](repeating: systemLevel, count: 960), at: timestamp, from: .system,
                                         systemID: i < 20 ? initialSystem : restartedSystem)
                    }
                } else {
                    recorder.enqueue([Float](repeating: 0.5, count: 960), at: timestamp, from: .system)
                }
            }
            if fail && i == 88 {
                recorder.enqueue([0.2], at: .nan, from: .mic)
                return
            }
            i += 1
            let remaining = (start + Double(i) * 0.02) - CMClockGetTime(CMClockGetHostTimeClock()).seconds
            if remaining > 0 { try? await Task.sleep(nanoseconds: UInt64(remaining * 1e9)) }
        }
    }
}
