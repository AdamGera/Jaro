import AVFoundation
import CoreAudio
import Foundation
import ScreenCaptureKit

private func check(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw CaptureError("Test failed: " + message) }
}

private func checkMix() throws {
    var both = Timeline(mode: .both)
    both.add([0.5, 1, 1], at: 1, from: .mic)
    both.add([-1, 1, 0.75], at: 2, from: .system)
    try check(both.render(through: 3) == [0, 8192, 0], "input offset and overlap")
    try check(both.render(through: 7) == [32767, 12288, 0, 0], "peak, trailing channel, silence")
    both.add([1, 1, 1], at: 0, from: .system)
    try check(both.lateFrames == 3, "dropped late samples reported")
    try check(both.systemEnd == 5, "stale samples cannot move the source's clock backwards")
    for (source, mode) in [(Source.mic, CaptureMode.mic), (.system, .system)] {
        var only = Timeline(mode: mode)
        only.add([1, -1], at: 0, from: source)
        try check(only.render(through: 2) == [32767, -32767], "single-source gain")
    }
}

private func checkMeetingAudioLevel() throws {
    var level = MeetingAudioLevel()
    try check(level.rms(at: 1) == 0, "the meeting starts without an audio level")
    level.observe([1, 0, 0, 0], from: .system, endingAt: 1)
    level.observe([0.25, -0.25], from: .mic, endingAt: 1.2)
    try check(level.rms(at: 1.25) == 0.5, "use the louder RMS rather than the peak or combined signal")
    try check(level.rms(at: 1.31) == 0.25, "the fresh microphone remains when Mac audio goes stale")
    level.observe([1], from: .system, endingAt: 0.95)
    try check(level.rms(at: 1.31) == 0.25, "a late buffer never replaces a newer level")
    try check(level.rms(at: 1.51) == 0, "the meter goes quiet when both sources have stopped")
    level.observe([0.75], from: .system, endingAt: 2)
    try check(level.rms(at: 2) == 0.75, "Mac audio alone restarts the level")
}

private func checkMeetingInputRoute() throws {
    try check(meetingInputMatches("com.google.chrome.canary", bundle: "com.google.Chrome.Canary.helper", processName: nil),
              "the browser helper belongs to its actual browser variant")
    try check(!meetingInputMatches("com.google.chrome.canary", bundle: "com.google.Chrome.helper", processName: nil),
              "a Chromium variant does not adopt another browser's microphone")
    try check(!meetingInputMatches("com.google.chrome", bundle: "com.google.Chrome.Canary.helper", processName: nil),
              "the stable browser does not adopt a variant's microphone")
    try check(meetingInputMatches("com.apple.safari", bundle: "com.apple.WebKit.GPU", processName: "Safari Graphics and Media"),
              "Safari can follow its named WebKit input")
    try check(!meetingInputMatches("com.apple.safari", bundle: "com.apple.WebKit.GPU", processName: "Mail Graphics and Media"),
              "a different WebKit client is not Safari")
    try check(meetingInputMatches("us.zoom.xos", bundle: "us.zoom.CptHost", processName: nil) &&
              meetingInputMatches("us.zoom.caphost", bundle: "us.zoom.ZoomCefHelper", processName: nil),
              "the Zoom input follows its call helpers")
    try check(meetingInputMatches("com.apple.facetime", bundle: "com.apple.avconferenced", processName: nil),
              "an established FaceTime recording can follow its audio service")
    let builtIn = InputDevice(id: 1, name: "Built-in", uid: "internal", transport: 0)
    let saved = InputDevice(id: 2, name: "Saved", uid: "saved", transport: 0)
    let headset = InputDevice(id: 3, name: "Headset", uid: "headset", transport: 0)
    let all = [builtIn, saved, headset]
    var route = MeetingInputRoute()
    route.observeCall([headset.uid])
    route.observeCall([])
    route.observeCall([builtIn.uid, headset.uid])
    let plan = route.candidates(savedUID: saved.uid, available: all, system: builtIn)
    try check(plan == [headset, saved, builtIn], "pin the call input during mute and across reordered reports")
    try check(route.candidates(savedUID: saved.uid, available: [saved, builtIn], system: builtIn) == [saved, builtIn],
              "unavailable call input falls back to the saved microphone")
    route.observeCall([builtIn.uid])
    let changed = route.candidates(savedUID: saved.uid, available: all, system: builtIn)
    try check(changed == [builtIn, saved], "follow the replacement call route and deduplicate the system default")
    try check(route.canOpen(plan, at: 0), "first open is immediate")
    var now = 0.0
    for delay: Double in [1, 2, 4, 8, 16, 30, 30, 30] {
        route.retry(after: now, hasWorkingInput: true)
        try check(!route.canOpen(plan, at: now + delay - 0.01), "failed preference waits before retry")
        now += delay
        try check(route.canOpen(plan, at: now), "retry is due")
    }
    route.retry(after: now, hasWorkingInput: true)
    try check(!route.canOpen(plan, at: now + 100), "stop retrying an unopenable preference while a fallback works")
    try check(route.canOpen(changed, at: now), "a device change resets the budget")
    try check(route.candidates(savedUID: nil, available: [], system: .unavailable).isEmpty,
              "no device does not fabricate a route")
}

private struct FixtureSystemAudio: MeetingSystemAudioConnection {
    let read: () -> MeetingSystemAudioReadiness
    var readiness: MeetingSystemAudioReadiness { read() }
    let closed: () async -> Void
    func stop() async { await closed() }
}

private func checkMeetingSystemAudioReadiness() throws {
    var tap = MeetingSystemAudioHealth(openedAt: 100)
    try check(tap.readiness(for: .tap, at: 102) == .waiting, "opening a tap does not establish audio delivery")
    try check(!tap.receive([], timestamp: 102, at: 102), "an empty buffer does not establish contact")
    try check(tap.readiness(for: .tap, at: 103) == .failed, "empty callbacks do not extend the startup deadline")

    tap = MeetingSystemAudioHealth(openedAt: 100)
    let silence = [Float](repeating: 0, count: 512)
    try check(tap.receive(silence, timestamp: 102, at: 102), "silent PCM is valid")
    try check(tap.readiness(for: .tap, at: 104) == .ready, "a real buffer brings the tap online")
    try check(!tap.receive([], timestamp: 104, at: 104), "empty buffers do not confirm ongoing delivery")
    try check(tap.readiness(for: .tap, at: 105) == .failed, "the running tap detects stalled audio")

    let fifteenSeconds = [Float](repeating: 0, count: 15 * Timeline.rate)
    for backend in [MeetingSystemAudioMode.tap, .screen] {
        var health = MeetingSystemAudioHealth(openedAt: 0)
        _ = health.receive(fifteenSeconds, timestamp: 15, at: 15)
        try check(health.readiness(for: backend, at: 15) == .ready, "idle audio must not cycle capture")
        _ = health.receive([0.2], timestamp: 16, at: 16)
        _ = health.receive(Array(fifteenSeconds.dropLast()), timestamp: 31, at: 31)
        try check(health.readiness(for: backend, at: 31) == .ready, "less than 15 seconds is allowed")
        _ = health.receive([0, 0.000001, 0], timestamp: 31, at: 31)
        _ = health.receive(Array(fifteenSeconds.dropLast(2)), timestamp: 46, at: 46)
        try check(health.readiness(for: backend, at: 46) == .ready, "quiet speech resets the zero count")
        _ = health.receive([0], timestamp: 46, at: 46)
        try check(health.readiness(for: backend, at: 46) == .failed, "continuous silent packets can mask a lost output")
    }

    var initiallyEmpty = MeetingOutputRoute(at: 10, uid: "")
    try check(!initiallyEmpty.changed(at: 12) { "speakers" }, "empty output IDs are unavailable")
    var route = MeetingOutputRoute(at: 10, uid: nil)
    try check(!route.changed(at: 11) { fatalError("route checked too often") }, "route checks are bounded")
    try check(!route.changed(at: 12) { "speakers" }, "adopt the first readable output")
    try check(!route.changed(at: 14) { nil }, "temporary output lookup failure is not a route change")
    try check(!route.changed(at: 16) { "speakers" }, "same output does not interrupt capture")
    try check(route.changed(at: 18) { "headphones" }, "rebuild when the hardware output changes")

    var screen = MeetingSystemAudioHealth(openedAt: 100)
    try check(screen.readiness(for: .screen, at: 150) == .ready, "ScreenCaptureKit is allowed to remain quiet")
    try check(!screen.receive(silence, timestamp: .nan, at: 150), "invalid timing rejects the audio")
    try check(screen.readiness(for: .screen, at: 150) == .failed, "invalid timing needs recovery on ScreenCaptureKit too")
}

private func checkMeetingSystemAudioSupervisor() async throws {
    var attempts = [MeetingSystemAudioMode]()
    var availability = [Bool]()
    var delays = [Duration]()
    var closed = 0
    var firstScreenPoll = true
    let closeLock = NSLock()
    let (retirement, didRetire) = AsyncStream<Void>.makeStream()
    var retirements = retirement.makeAsyncIterator()
    await superviseMeetingSystemAudio(mode: .tapAndScreen, hostPID: nil, receive: { _, _, _ in },
                                    change: { available, _ in availability.append(available) },
                                    open: { backend, _, _ in
        attempts.append(backend)
        if backend == .tap { throw CaptureError("The fixture backend could not open.") }
        return FixtureSystemAudio(read: {
            defer { firstScreenPoll = false }
            return firstScreenPoll ? .ready : .failed
        }) { didRetire.yield(()) }
    }, pause: { duration in
        delays.append(duration)
        return true
    }, waitForRetry: { duration, _ in
        delays.append(duration)
        try? await Task.sleep(for: .milliseconds(25))
        return .cancelled
    })
    _ = await retirements.next()
    try check(attempts == [.tap, .screen], "prefer a tap and fall back only when authorized")
    try check(availability == [false, true, false], "publish outages and a confirmed fallback in order")
    try check(delays == [.milliseconds(250), .seconds(1)], "close the failed source and back off")

    attempts.removeAll()
    closed = 0
    var status = [String]()
    var poll = 0
    let (tapMayClose, closeTap) = AsyncStream<Void>.makeStream()
    await superviseMeetingSystemAudio(mode: .tapAndScreen, hostPID: nil, receive: { _, _, _ in },
                                    change: { available, backend in status.append(available ? backend?.rawValue ?? "" : "unavailable") },
                                    open: { backend, _, _ in
        attempts.append(backend)
        return FixtureSystemAudio(read: {
            if backend == .screen { return .ready }
            poll += 1
            return poll < 3 ? .waiting : .failed
        }) {
            if backend == .tap {
                var gate = tapMayClose.makeAsyncIterator()
                _ = await gate.next()
            }
            closeLock.withLock { closed += 1 }
            didRetire.yield(())
        }
    }, pause: { _ in attempts.count == 1 })
    try check(closeLock.withLock { closed == 1 }, "fallback can run while the retired tap is still closing")
    closeTap.yield(())
    _ = await retirements.next()
    _ = await retirements.next()
    try check(attempts == [.tap, .screen] && closeLock.withLock { closed == 2 },
              "a tap without valid buffers yields to the authorized screen backend")
    try check(status == ["unavailable", "screen"], "a stalled tap never clears the recovery notice")

    attempts.removeAll()
    closeLock.withLock { closed = 0 }
    availability.removeAll()
    await superviseMeetingSystemAudio(mode: .tap, hostPID: nil, receive: { _, _, _ in },
                                    change: { ready, _ in availability.append(ready) },
                                    open: { backend, _, _ in
        attempts.append(backend)
        return FixtureSystemAudio(read: { .waiting }) { closeLock.withLock { closed += 1 } }
    }, pause: { _ in false })
    try check(attempts == [.tap] && closed == 1 && availability.isEmpty,
              "stopping during the startup wait closes the tap without publishing readiness or opening ScreenCaptureKit")
}

private func checkMeetingSystemAudioPermissionDenial(_ denial: Error) async throws {
    var attempts = [MeetingSystemAudioMode]()
    var reports = 0
    var waits = 0
    await superviseMeetingSystemAudio(mode: .tapAndScreen, hostPID: nil, receive: { _, _, _ in },
                                    change: { _, _ in }, permissionDenied: { reports += 1 },
                                    open: { backend, _, _ in
        attempts.append(backend)
        if backend == .screen { throw denial }
        throw CaptureError("The audio tap is unavailable.")
    }, waitForRetry: { _, _ in waits += 1; return .cancelled })
    try check(attempts == [.tap, .screen] && reports == 1 && waits == 0,
              "explicit screen denial stops retrying after the transient tap failure")

    let (retired, didRetire) = AsyncStream<Void>.makeStream()
    var retires = retired.makeAsyncIterator()
    var current = MeetingSystemAudioReadiness.ready
    var availability = [Bool]()
    await superviseMeetingSystemAudio(mode: .screen, hostPID: nil, receive: { _, _, _ in },
                                    change: { available, _ in availability.append(available) },
                                    permissionDenied: { reports += 1 },
                                    open: { _, _, _ in FixtureSystemAudio(read: { current }) { didRetire.yield(()) } },
                                    pause: { _ in current = .permissionDenied; return true },
                                    waitForRetry: { _, _ in waits += 1; return .cancelled })
    _ = await retires.next()
    try check(availability == [true] && reports == 2 && waits == 0,
              "permission lost during a meeting retires the stream without entering reconnect")
}

private func checkMeetingSystemAudioOpenTimeout() async throws {
    let (resumeOpen, opener) = AsyncStream<Void>.makeStream()
    let (closed, didClose) = AsyncStream<Void>.makeStream()
    let (packets, audio) = AsyncStream<Void>.makeStream()
    var starts = 0
    var availability = [Bool]()
    var delays = [Duration]()
    await superviseMeetingSystemAudio(mode: .tap, hostPID: nil, receive: { _, _, _ in audio.yield(()) },
                                    change: { ready, _ in availability.append(ready) },
                                    open: { _, _, forward in
        starts += 1
        forward([0.75], hostSeconds())
        var wait = resumeOpen.makeAsyncIterator()
        _ = await wait.next()
        forward([0.75], hostSeconds())
        return FixtureSystemAudio(read: { .ready }) { didClose.yield(()) }
    }, openTimeout: .milliseconds(200), waitForRetry: { duration, _ in
        delays.append(duration)
        return delays.count == 1 ? .elapsed : .cancelled
    })
    try check(starts == 1 && availability == [false] && delays == [.seconds(1), .seconds(2)],
              "retry does not start a second copy of a timed-out backend")
    var replacementStarts = 0
    await superviseMeetingSystemAudio(mode: .tap, hostPID: nil, receive: { _, _, _ in }, change: { _, _ in },
                                    open: { _, _, _ in
        replacementStarts += 1
        throw CaptureError("The timed-out tap should still hold its slot.")
    }, waitForRetry: { _, _ in .cancelled })
    try check(replacementStarts == 0, "a new meeting in the same recorder cannot duplicate an unresolved tap")

    var recovered = [Bool]()
    var reopened = false
    await superviseMeetingSystemAudio(mode: .tap, hostPID: nil, receive: { _, _, _ in },
                                    change: { ready, _ in recovered.append(ready) },
                                    open: { _, _, _ in
        reopened = true
        return FixtureSystemAudio(read: { .ready }) {}
    }, pause: { _ in false }, consoleAvailable: { true }, waitForRetry: { _, available in
        opener.yield(())
        let result = await waitForMeetingSystemAudioRetry(.seconds(2), becameAvailable: available)
        return result == .available ? .available : .cancelled
    })
    try check(reopened && recovered == [false, true], "slot cleanup wakes a waiting meeting before its backoff expires")
    var cleanup = closed.makeAsyncIterator()
    _ = await cleanup.next()
    audio.finish()
    var delivered = packets.makeAsyncIterator()
    let packet: Void? = await delivered.next()
    try check(packet == nil, "an abandoned backend cannot deliver packets before or after its open returns")

    let (openStarted, didStart) = AsyncStream<Void>.makeStream()
    let (continueCancelled, releaseCancelled) = AsyncStream<Void>.makeStream()
    let (closedCancelled, didCloseCancelled) = AsyncStream<Void>.makeStream()
    let supervisor = Task {
        await superviseMeetingSystemAudio(mode: .screen, hostPID: nil, receive: { _, _, _ in },
                                        change: { _, _ in preconditionFailure("Cancelled open reported a status") },
                                        open: { _, _, _ in
            didStart.yield(())
            var wait = continueCancelled.makeAsyncIterator()
            _ = await wait.next()
            return FixtureSystemAudio(read: { .ready }) { didCloseCancelled.yield(()) }
        })
    }
    var starting = openStarted.makeAsyncIterator()
    _ = await starting.next()
    supervisor.cancel()
    await supervisor.value
    releaseCancelled.yield(())
    var cancelledCleanup = closedCancelled.makeAsyncIterator()
    _ = await cancelledCleanup.next()
}

private func checkMeetingSystemAudioConsoleRecovery() async throws {
    var console = false
    var delays = [Duration]()
    var woken = false
    await superviseMeetingSystemAudio(mode: .tap, hostPID: nil, receive: { _, _, _ in }, change: { _, _ in },
                                    open: { _, _, _ in throw CaptureError("The fixture has no audio.") },
                                    consoleAvailable: { console }, waitForRetry: { delay, available in
        delays.append(delay)
        if delays.count == 3 {
            console = true
            woken = available()
            return woken ? .available : .cancelled
        }
        return delays.count < 3 ? .elapsed : .cancelled
    })
    try check(woken && delays == [.seconds(1), .seconds(2), .seconds(4), .seconds(1)],
              "console recovery advances the retry and resets only the accumulated delay")

    console = false
    var opened = [MeetingSystemAudioMode]()
    var polls = 0
    await superviseMeetingSystemAudio(mode: .tapAndScreen, hostPID: nil, receive: { _, _, _ in }, change: { _, _ in },
                                    open: { backend, _, _ in
        opened.append(backend)
        if backend == .tap { throw CaptureError("The fixture tap is unavailable.") }
        return FixtureSystemAudio(read: { .ready }) {}
    }, pause: { _ in
        polls += 1
        console = true
        return polls == 1
    }, consoleAvailable: { console }, waitForRetry: { _, _ in .cancelled })
    try check(opened == [.tap, .screen] && polls == 2, "unlock leaves a healthy fallback recording")
}

private func checkMeetingMicrophoneSilence() throws {
    var input = MeetingMicrophoneSilence()
    let owners: Set<pid_t> = [10, 11]
    input.observe(from: 0, to: 2, lastSignalEnd: nil)
    input.observe(from: 2.1, to: 4.1, lastSignalEnd: nil)
    try check(input.poll(at: 4.1, callUsesInput: true, activeOwners: owners) == nil,
              "missing mic samples cannot count as silence")
    input.observe(from: 4.1, to: 5.2, lastSignalEnd: nil)
    try check(input.poll(at: 5.2, callUsesInput: false, activeOwners: owners) == nil,
              "a call on another route cannot trigger a reopen")
    try check(input.poll(at: 6, callUsesInput: true, activeOwners: owners) == nil,
              "stale evidence cannot trigger a reopen")
    try check(input.poll(at: 5.2, callUsesInput: true, activeOwners: owners) == .quick && !input.warning,
              "try recovering three seconds of contiguous digital silence before warning")
    input.beginRecovery(.quick, at: 5.2)
    input.observe(from: 6, to: 9, lastSignalEnd: nil)
    try check(input.poll(at: 9, callUsesInput: true, activeOwners: owners) == .quick,
              "a second quick reopen is available")
    input.beginRecovery(.quick, at: 9)
    _ = input.poll(at: 9.5, callUsesInput: true, activeOwners: [10])
    input.observe(from: 10, to: 13, lastSignalEnd: nil)
    try check(input.poll(at: 13, callUsesInput: true, activeOwners: owners) == nil && input.warning,
              "warn when the quick budget is spent and ignore releases during the clock reset")
    try check(input.poll(at: 13, callUsesInput: true, activeOwners: nil) == nil,
              "a failed CoreAudio snapshot does not count as an app release")
    try check(input.poll(at: 13, callUsesInput: false, activeOwners: [11]) == .released,
              "release by the call app can authorize a retry")
    input.deferRecovery(.released)
    try check(input.poll(at: 13, callUsesInput: false, activeOwners: [11]) == .released && input.warning,
              "dictation defers the recovery without spending it")
    input.beginRecovery(.released, at: 13)
    for index in 0..<2 {
        let now = 18.0 + 5.0 * Double(index)
        input.observe(from: now - 4, to: now, lastSignalEnd: nil)
        _ = input.poll(at: now, callUsesInput: true, activeOwners: owners)
        try check(input.poll(at: now, callUsesInput: true, activeOwners: [10]) == .released,
                  "an external release permits another retry until the episode cap")
        input.beginRecovery(.released, at: now)
    }
    input.observe(from: 24, to: 28, lastSignalEnd: nil)
    _ = input.poll(at: 28, callUsesInput: true, activeOwners: owners)
    try check(input.poll(at: 28, callUsesInput: true, activeOwners: [10]) == nil && input.warning,
              "three release retries per warning episode")
    input.changeDevice()
    try check(input.warning && input.poll(at: 28, callUsesInput: true, activeOwners: owners) == nil,
              "a route change discards old evidence but keeps the warning until signal returns")
    input.observe(from: 28, to: 28.1, lastSignalEnd: 28.00001)
    try check(!input.warning, "one nonzero sample clears the warning")
    input.observe(from: 28.1, to: 31.1, lastSignalEnd: nil)
    try check(input.poll(at: 31.1, callUsesInput: true, activeOwners: owners) == nil && input.warning,
              "signal does not replenish the recording's quick reopen budget")
    try check(input.poll(at: 31.1, callUsesInput: true, activeOwners: [10]) == .released,
              "signal replenishes the per-episode release retry budget")
}

private func checkDictationReleaseTail() throws {
    let frame = DictationVoice.frameSize
    let release = 25 * frame + 19
    let spoken = (0..<frame).map { Float(0.08 * sin(Double($0) * 0.07)) + 0.2 }
    let quiet = [Float](repeating: 0.2, count: frame)
    func cutoff(_ ranges: [Range<Int>], captured: Int = 60) -> Int? {
        var timeline = Timeline(mode: .mic)
        var voice = DictationVoice()
        for index in 0..<captured {
            let active = ranges.contains { $0.contains(index) }
            timeline.add(active ? spoken : quiet, at: index * frame, from: .mic)
            voice.observe(timeline)
            _ = timeline.render(through: max(0, (index - 50) * frame))
        }
        return voice.cutoff(after: release, through: timeline.micEnd)
    }
    try check(cutoff([13..<29], captured: 35) == nil, "wait for the exact minimum tail frame")
    try check(cutoff([13..<29]) == release + DictationVoice.base, "short trailing syllable uses the minimum tail")
    try check(cutoff([34..<35]) == release + DictationVoice.base, "a click does not grow the tail")
    try check(cutoff([13..<32]) == release + DictationVoice.base, "three quiet frames break speech before the minimum tail")
    try check(cutoff([13..<33]) == 43 * frame, "two quiet frames allow speech to carry into the tail")
    try check(cutoff([13..<39, 41..<42]) == 50 * frame, "a brief inter-syllable gap does not end the tail")
    try check(cutoff([13..<57]) == release + DictationVoice.cap, "ongoing speech cannot grow beyond the cap")
}

private func checkWarmClockRecovery() throws {
    func range(_ low: Double, _ high: Double) -> AudioValueRange { AudioValueRange(mMinimum: low, mMaximum: high) }
    let common = [range(44_100, 48_000)]
    try check(alternateMicrophoneRate(from: 48_000, supported: common) == 44_100, "prefer 44.1 kHz when coming from 48 kHz")
    try check(alternateMicrophoneRate(from: 44_100, supported: common) == 48_000, "prefer 48 kHz when supported")
    try check(alternateMicrophoneRate(from: 24_000, supported: [range(16_000, 24_000)]) == 16_000, "use the advertised fallback")
    try check(alternateMicrophoneRate(from: 48_000, supported: [range(48_000, 48_000)]) == nil, "single-rate input reopens normally")
    try check(resetMicrophoneClock(.unavailable) == .unavailable, "missing device cannot reset its clock")
    var reads = [UInt32(1), 0].makeIterator()
    try check(microphoneClockIdle(readRunning: { reads.next() }, pause: {}), "wait for the retired stream to stop")
    try check(!microphoneClockIdle(readRunning: { 1 }, pause: {}), "skip changing the clock of an active device")
    try check(!microphoneClockIdle(readRunning: { nil }, pause: {}), "skip when device activity cannot be read")

    var writes: [Double] = []
    var resets = 0
    var result = cycleMicrophoneClock(original: 48_000, alternate: 44_100, set: { writes.append($0); return true },
                                     settled: { _ in true }, prime: { resets += 1; return true })
    try check(result == .reset && writes == [44_100, 48_000] && resets == 1, "prime exactly once and restore the original clock")
    writes.removeAll()
    result = cycleMicrophoneClock(original: 48_000, alternate: 44_100, set: { writes.append($0); return true },
                                 settled: { $0 == 48_000 }, prime: { resets += 1; return false })
    try check(result == .failed && writes == [44_100, 48_000] && resets == 1, "restore when the alternate failed to settle")
    writes.removeAll()
    result = cycleMicrophoneClock(original: 48_000, alternate: 44_100, set: { writes.append($0); return true },
                                 settled: { _ in true }, prime: { false })
    try check(result == .failed && writes == [44_100, 48_000], "restore even when the throwaway stream failed")
    writes.removeAll()
    result = cycleMicrophoneClock(original: 48_000, alternate: 44_100, set: { writes.append($0); return true },
                                 settled: { $0 == 44_100 }, prime: { true })
    try check(result == .restoreFailed && writes == [44_100, 48_000, 48_000], "bounded restore failures are visible")
}

private func checkClockDrift() throws {
    // Two independent clocks drift in opposite directions. The later callback for the
    // earlier-timestamped source still has to align with its partner on the host timeline.
    let seconds = 120, rate = Timeline.rate
    let markers = [2 * rate + 192, 65 * rate + 192, 118 * rate + 192]
    var found = [Int]()
    var timeline = Timeline(mode: .both)
    for block in 0..<(seconds * 50 + 2) {
        for (source, drift) in [(Source.system, 1.000140), (.mic, 0.999860)] {
            let start = Double(block * 960) * drift
            var samples = [Float](repeating: 0, count: 960)
            for mark in markers {
                let sample = Int(((Double(mark) - start) / drift).rounded())
                if samples.indices.contains(sample) {
                    // A short pulse on each channel gives the overlap a unique higher level.
                    for index in sample..<min(sample + 9, samples.count) { samples[index] = 1 }
                }
            }
            timeline.add(samples, at: Int(start.rounded()), from: source)
        }
        let start = timeline.cursor
        let mixed = timeline.render(through: max(0, block * 960 - rate))
        for i in mixed.indices where mixed[i] == 32767 { found.append(start + i) }
    }
    for mark in markers {
        try check(found.contains(where: { abs($0 - mark) <= 4 }), "capture clocks remain aligned at frame \(mark)")
    }
}

private func checkConverter() throws {
    for rate in [16_000, 24_000, 44_100, 48_000] {
        let format = AVAudioFormat(standardFormatWithSampleRate: Double(rate), channels: 2)!
        let packet = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: UInt32(rate / 50))!
        packet.frameLength = packet.frameCapacity
        for i in 0..<Int(packet.frameLength) {
            packet.floatChannelData![0][i] = 0.25
            packet.floatChannelData![1][i] = 0.25
        }
        let converter = MonoConverter()
        var frames = 0
        var valid = true
        for i in 0..<100 {
            try converter.append(packet, at: Double(i) / 50) { mono, time in
                valid = valid && abs(time * Double(Timeline.rate) - Double(frames)) < 0.001
                    && abs((mono.last ?? 0) - 0.25) < 0.05
                frames += mono.count
            }
        }
        try converter.finish { mono, _ in frames += mono.count }
        try check(valid, "level and packet continuity at \(rate) Hz")
        try check(abs(frames - 2 * Timeline.rate) <= 1, "resampled duration at \(rate) Hz")
    }
}

// Actual mic taps can deliver 100 ms despite the requested 1024-frame tap size.
private func checkMicContinuity() throws {
    let rate = Timeline.rate, packetSize = rate / 10
    let format = AVAudioFormat(standardFormatWithSampleRate: Double(rate), channels: 1)!
    let packet = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: UInt32(packetSize))!
    packet.frameLength = packet.frameCapacity
    let converter = MonoConverter()
    var timeline = Timeline(mode: .mic)
    var expected = [Int16]()
    func collect(_ samples: [Float], _ time: Double) {
        timeline.add(samples, at: Int((time * Double(rate)).rounded()), from: .mic)
    }
    for i in 0..<441 {
        for j in 0..<packetSize {
            let phase = Double(i * packetSize + j) * 2 * Double.pi * 220 / Double(rate)
            let sample = Float(0.21 + 0.05 * sin(phase))
            packet.floatChannelData![0][j] = sample
            expected.append(Int16((sample * 32767).rounded()))
        }
        try converter.append(packet, at: Double(i) / 10, emit: collect)
    }
    try converter.finish(emit: collect)
    let output = timeline.render(through: expected.count)
    let errors = zip(output, expected).filter { abs(Int($0) - Int($1)) > 1 }.count
    try check(errors == 0, "100 ms mic packets have \(errors) incorrect samples, including the final packet")
    print("44.1 s mic tone: all \(output.count) samples match the reference.")
}

private func checkInputGap() throws {
    let format = AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 1)!
    let packet = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 4800)!
    packet.frameLength = 4800
    let converter = MonoConverter()
    var timeline = Timeline(mode: .mic)
    func collect(_ samples: [Float], _ time: Double) {
        timeline.add(samples, at: Int((time * 48000).rounded()), from: .mic)
    }
    packet.floatChannelData![0].initialize(repeating: 0.25, count: 4800)
    try converter.append(packet, at: 0, emit: collect)
    packet.floatChannelData![0].initialize(repeating: -0.25, count: 4800)
    try converter.append(packet, at: 0.15, emit: collect)
    try converter.finish(emit: collect)
    let output = timeline.render(through: 12000)
    try check(output[..<4800].allSatisfy { $0 == 8192 } &&
              output[4800..<7200].allSatisfy { $0 == 0 } &&
              output[7200...].allSatisfy { $0 == -8192 }, "preserve real input gap and both converter tails")
}

private func checkConvertedSync() throws {
    let rate = Timeline.rate
    let markers = [2.047, 65.057, 118.043]
    var onsets = [Source: [Int]]()
    var baseMix = Timeline(mode: .both)
    for source in [Source.mic, .system] {
        let mic = source == .mic
        let hz = mic ? 44100 : 48000, size = mic ? 4410 : 512
        let offset = mic ? 0.137 : 0.021, drift = mic ? 0.99986 : 1.00014
        let channels = mic ? 1 : 2
        let format = AVAudioFormat(standardFormatWithSampleRate: Double(hz), channels: UInt32(channels))!
        let packet = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: UInt32(size))!
        packet.frameLength = packet.frameCapacity
        let converter = MonoConverter()
        var single = Timeline(mode: mic ? .mic : .system)
        var edges = [Int]()
        var high = false
        func collect(_ samples: [Float], _ time: Double) {
            let frame = Int((time * Double(rate)).rounded())
            single.add(samples, at: frame, from: source)
            baseMix.add(samples, at: frame, from: source)
            let start = single.cursor
            for (i, sample) in single.render(through: frame - rate / 4).enumerated() {
                if !high && sample > 8000 { edges.append(start + i) }
                high = sample > 8000
            }
        }
        // A shared 10 ms marker occurs at the same host time on independent clocks.
        for block in 0..<Int(ceil(120 * Double(hz) / Double(size))) {
            let t = offset + Double(block * size) * drift / Double(hz)
            for c in 0..<channels { packet.floatChannelData![c].initialize(repeating: 0, count: size) }
            for marker in markers where marker < t + Double(size) * drift / Double(hz) && marker + 0.01 > t {
                let lo = max(0, Int(ceil((marker - t) * Double(hz) / drift)))
                let hi = min(size, Int(ceil((marker + 0.01 - t) * Double(hz) / drift)))
                if hi > lo {
                    for c in 0..<channels { for j in lo..<hi { packet.floatChannelData![c][j] = 0.5 } }
                }
            }
            try converter.append(packet, at: t, emit: collect)
        }
        try converter.finish(emit: collect)
        try check(edges.count == markers.count, "all \(source) markers survive")
        onsets[source] = edges
    }
    let mix = baseMix.render(through: 120 * rate)
    var worst = 0
    for (i, marker) in markers.enumerated() {
        let mic = onsets[.mic]![i], system = onsets[.system]![i]
        let target = Int((marker * Double(rate)).rounded())
        worst = max(worst, abs(mic - system))
        try check(abs(target - mic) < 48 && abs(target - system) < 48, "both markers within 1 ms of host time")
        try check(mix[target + 240] > 15000, "mic and system markers overlap in the final mix")
    }
    try check(worst < 48, "mic/system offset under 1 ms")
    print("120 s independently clocked inputs: maximum marker offset \(worst) frames at 48 kHz.")
}

private func checkFile(directory: URL, seconds: Int) throws {
    let path = directory.appendingPathComponent("synthetic.wav").path
    let sink = try WaveSink(path: path)
    var timeline = Timeline(mode: .both)
    let second = Timeline.rate
    for i in 0..<seconds {
        // Known near- and far-end pulses verify offsets over the entire run.
        timeline.add([0.5, 0.5], at: i * second + 12, from: .mic)
        timeline.add([-0.5], at: i * second + second - 2, from: .system)
        for end in 1...4 {
            try sink.append(timeline.render(through: i * second + end * (second / 4)))
        }
    }
    try sink.finish()
    let url = URL(fileURLWithPath: path)
    let info = try FileManager.default.attributesOfItem(atPath: path)
    try check((info[.size] as? Int) == 44 + seconds * second * 2, "exact WAV size")
    let reader = try AVAudioFile(forReading: url)
    try check(reader.length == Int64(seconds * second), "Core Audio reads full duration")
    let file = try FileHandle(forReadingFrom: url)
    defer { try? file.close() }
    for i in [0, seconds / 2, seconds - 1] {
        for (offset, expected) in [(12, Int16(8192)), (second - 2, Int16(-8192))] {
            try file.seek(toOffset: UInt64(44 + (second * i + offset) * 2))
            let data = try file.read(upToCount: 2)!
            let sample = data.withUnsafeBytes { $0.loadUnaligned(as: Int16.self) }
            try check(sample == expected, "known sample at second \(i), offset \(offset)")
        }
    }
    do {
        _ = try WaveSink(path: path)
        throw CaptureError("Existing file was replaced.")
    } catch let e as CaptureError where e.message == "Choose an empty recording file." {}
    if seconds == 10800 {
        try check(timeline.render(through: (seconds + 1) * second).isEmpty, "three-hour cap")
    }
    print("Verified \(seconds)s and \(sink.frames) samples on disk, with labeled mic/system markers.")
}

private func checkColdMicrophoneOpen() async throws {
    let preferred = InputDevice(id: 9, name: "USB microphone", uid: "usb", transport: 0)
    let fallback = InputDevice(id: 5, name: "System microphone", uid: "default", transport: 0)
    for failures in 0...3 {
        var attempts = [InputDevice]()
        var waits = [Duration]()
        let result = try await openColdMicrophone(
            selected: { preferred }, systemDefault: { fallback }, stopped: { false },
            open: { device in
                attempts.append(device)
                if device == preferred && attempts.count <= failures { throw CaptureError("Input still waking.") }
            },
            pause: { waits.append($0) }
        )
        let expectedAttempts = Array(repeating: preferred, count: min(failures + 1, 3)) + (failures == 3 ? [fallback] : [])
        try check(attempts == expectedAttempts, "try the preferred microphone before the system input")
        try check(waits == Array(repeating: .milliseconds(100), count: min(failures, 2)), "retry spacing")
        try check(result == (failures == 3 ? .systemDefault : .selected), "report the successful microphone route")
    }
    for selection in [nil, InputDevice.unavailable] {
        var attempts = [InputDevice]()
        let result = try await openColdMicrophone(
            selected: selection.map { device in { device } }, systemDefault: { fallback }, stopped: { false },
            open: { attempts.append($0) },
            pause: { _ in throw CaptureError("Default should not wait for a missing device.") }
        )
        try check(result == .systemDefault && attempts == [fallback], "automatic and unlisted selections open the default directly")
    }
    var stopped = false
    var attempts = [InputDevice]()
    let stoppedDuringRetry = try await openColdMicrophone(
        selected: { preferred }, systemDefault: { fallback }, stopped: { stopped },
        open: { device in attempts.append(device); throw CaptureError("Input still waking.") },
        pause: { _ in stopped = true }
    )
    try check(stoppedDuringRetry == .abandoned && attempts == [preferred], "release prevents further attempts")
    stopped = false
    let stoppedDuringOpen = try await openColdMicrophone(
        selected: nil, systemDefault: { fallback }, stopped: { stopped },
        open: { _ in stopped = true }
    )
    try check(stoppedDuringOpen == .abandoned, "a late successful open is abandoned after cancellation")
    attempts.removeAll()
    do {
        _ = try await openColdMicrophone(
            selected: { preferred }, systemDefault: { fallback }, stopped: { false },
            open: { device in attempts.append(device); throw RecordingPermissionError(source: .mic) }
        )
        throw CaptureError("Permission denial must reach the permission flow.")
    } catch is RecordingPermissionError {
        try check(attempts == [preferred], "permission denial skips retries and fallback")
    }
    attempts.removeAll()
    do {
        _ = try await openColdMicrophone(
            selected: { preferred }, systemDefault: { fallback }, stopped: { false },
            open: { device in
                attempts.append(device)
                throw CaptureError(device == fallback ? "Default also unavailable." : "Input still waking.")
            },
            pause: { _ in }
        )
        throw CaptureError("An unavailable default must fail the take.")
    } catch let error as CaptureError {
        try check(attempts == [preferred, preferred, preferred, fallback] && error.message == "Default also unavailable.",
                  "report the final device error once all routes fail")
    }
}

private func checkDictationInputRoute() async throws {
    let internalMic = InputDevice(id: 1, name: "Internal", uid: "BuiltInMicrophoneDevice", transport: kAudioDeviceTransportTypeBuiltIn)
    let usb = InputDevice(id: 2, name: "USB", uid: "usb", transport: kAudioDeviceTransportTypeUSB)
    let headset = InputDevice(id: 3, name: "Headset", uid: "headset", transport: kAudioDeviceTransportTypeBluetoothLE)
    let headsetJack = InputDevice(id: 4, name: "Headphone jack", uid: "BuiltInHeadphoneInputDevice", transport: kAudioDeviceTransportTypeBuiltIn)
    let olderJack = InputDevice(id: 5, name: "External microphone", uid: "BuiltInInputDevice",
                               transport: kAudioDeviceTransportTypeBuiltIn, inputDataSource: 0x656d6963)
    let lineIn = InputDevice(id: 6, name: "Line in", uid: "BuiltInLineSource",
                             transport: kAudioDeviceTransportTypeBuiltIn, inputDataSource: 0x6c696e65)
    let virtual = InputDevice(id: 7, name: "Virtual", uid: "virtual", transport: kAudioDeviceTransportTypeVirtual)
    let aggregate = InputDevice(id: 8, name: "Aggregate", uid: "aggregate", transport: kAudioDeviceTransportTypeAggregate)
    let phone = InputDevice(id: 9, name: "Phone", uid: "phone", transport: kAudioDeviceTransportTypeContinuityCaptureWireless)

    func route(_ saved: InputDevice? = nil, _ system: InputDevice, _ available: [InputDevice],
               closed: Bool = true, avoidBluetooth: Bool = false) -> DictationInputRoute {
        DictationInputRoute.resolve(savedUID: saved?.uid, available: available, systemDefault: system,
                                    lidClosed: closed, avoidBluetooth: avoidBluetooth)
    }
    let all = [internalMic, virtual, aggregate, phone, usb]
    try check(route(internalMic, internalMic, all).saved == nil, "a saved internal microphone stands aside with the lid closed")
    try check(route(internalMic, internalMic, all).system == usb, "the stand-in skips virtual, aggregate, and Continuity inputs")
    try check(route(nil, .unavailable, all, closed: false).primary == internalMic, "internal input is preferred when the default is missing with the lid open")
    try check(route(nil, internalMic, all, closed: false).primary == internalMic, "reopening restores the eligible default")
    try check(route(usb, internalMic, all).saved == usb, "closing the lid keeps an explicit external selection")
    for jack in [headsetJack, olderJack, lineIn] {
        try check(route(nil, internalMic, [internalMic, jack]).primary == jack, "wired jack remains usable with the lid closed")
    }
    try check(route(phone, internalMic, all).primary == phone, "an explicit Continuity microphone remains eligible")
    try check(route(nil, virtual, all).primary == virtual, "an explicit system-default virtual input remains eligible")
    try check(route(nil, headset, [headset, usb], avoidBluetooth: true).primary == usb, "Bluetooth preference picks another physical input")
    try check(route(nil, headset, [headset, internalMic], avoidBluetooth: true).primary == headset, "Bluetooth default remains usable as the last resort")
    try check(route(headset, headset, [headset, usb], avoidBluetooth: true).primary == headset, "an explicit headset stays selected")

    var opened = [InputDevice]()
    let system = try route(nil, internalMic, all, closed: false).openRecordingInput { opened.append($0) }
    try check(system == internalMic && opened == [internalMic], "ordinary recording uses the built-in system input")
    opened.removeAll()
    let missing = try route(usb, internalMic, [internalMic], closed: false).openRecordingInput { opened.append($0) }
    try check(missing == internalMic && opened == [internalMic], "a disconnected selection falls back to the internal microphone")
    opened.removeAll()
    let selected = route(usb, internalMic, all, closed: false)
    let preferred = try selected.openRecordingInput { opened.append($0) }
    try check(preferred == usb && opened == [usb], "ordinary recording uses an available saved input")
    opened.removeAll()
    let kept = try selected.openRecordingInput(keeping: internalMic) {
        opened.append($0)
        throw CaptureError("The saved input could not open.")
    }
    try check(kept == internalMic && opened == [usb], "a failed handoff keeps the working internal microphone")
    opened.removeAll()
    let avoided = try route(nil, headset, [internalMic, headset], closed: false, avoidBluetooth: true)
        .openRecordingInput { opened.append($0) }
    try check(avoided == internalMic && opened == [internalMic], "Bluetooth avoidance selects the built-in input")

    let blocked = route(internalMic, internalMic, [internalMic, virtual, aggregate, phone])
    var attempted = [InputDevice]()
    do {
        _ = try await openColdMicrophone(selected: { blocked.saved ?? .unavailable },
                                         systemDefault: { try blocked.require(blocked.system) }, stopped: { false },
                                         open: { attempted.append($0) })
        throw CaptureError("The closed-lid input was unexpectedly available.")
    } catch let error as CaptureError {
        try check(error.message == MicrophoneLid.unavailableMessage && attempted.isEmpty,
                  "cold dictation explains the lid and opens no input if only the internal microphone is usable")
    }
    print("Dictation input routing verified the lid, wired jacks, explicit routes, and physical fallback order.")
}

private func checkWarmMicrophoneRetry() throws {
    var open = WarmMicrophoneRetry()
    var start = 100.0
    for delay in [0.25, 0.5, 1, 2, 4, 5, 5] {
        open.openFailed(at: start)
        try check(!open.canOpen(at: start + delay - 0.01), "failed open waits before retry")
        start += delay
        try check(open.canOpen(at: start), "failed open retries within the 5-second ceiling")
    }
    open.opened()
    open.openFailed(at: start)
    try check(open.canOpen(at: start + 0.25), "successful open clears ordinary open failures")

    var stream = WarmMicrophoneRetry()
    start = 100
    for delay in [0.25, 0.5, 1, 2, 4, 8, 16, 32, 60, 60] {
        stream.opened()
        stream.streamFailed(at: start, steady: false)
        try check(!stream.canOpen(at: start + delay - 0.01), "a short-lived replacement keeps the stream backoff")
        try check(stream.canOpen(at: start, retryNow: true), "a route change or gesture can retry before the deadline")
        start += delay
        try check(stream.canOpen(at: start), "a stalled stream retries within the 60-second ceiling")
    }
    stream.openFailed(at: start)
    try check(!stream.canOpen(at: start + 59), "failed open during a stall retains the stall ceiling")
    stream.streamFailed(at: start, steady: true)
    try check(stream.canOpen(at: start), "a steady stream is replaced immediately")
    stream.openFailed(at: start)
    try check(stream.canOpen(at: start + 0.25), "steady delivery resets the open retry delay")

    var delivery = WarmMicrophoneDelivery(openedAt: 100)
    try check(!delivery.stalled(at: 103.999) && delivery.stalled(at: 104), "an open with no packets gets a 4-second grace period")
    for time in [101.0, 103, 106, 108] {
        let accepted = delivery.receive(at: time)
        try check(accepted && !delivery.steady, "an open becomes steady only after 8 seconds of delivery")
    }
    let accepted = delivery.receive(at: 109)
    try check(accepted && delivery.steady && !delivery.stalled(at: 112.99), "a continuous 8-second stream is steady")
    let late = delivery.receive(at: 113.1)
    try check(!late && delivery.stalled(at: 113.1) && delivery.steady, "a packet after an audio gap never revives a failed stream")
    var replacement = WarmMicrophoneDelivery(openedAt: 120)
    _ = replacement.receive(at: 120.5)
    try check(!replacement.steady, "a replacement stream must prove delivery on its own")
    print("Warm microphone verified open and stream backoff, urgent retry, and steady packet delivery.")
}

@main private enum Tests {
    static func main() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let denial = NSError(domain: SCStreamErrorDomain, code: SCStreamError.Code.userDeclined.rawValue)
        let wrapped = NSError(domain: NSCocoaErrorDomain, code: 1, userInfo: [NSUnderlyingErrorKey: denial])
        let multiple = NSError(domain: NSCocoaErrorDomain, code: 2, userInfo: [NSMultipleUnderlyingErrorsKey: [wrapped]])
        try check(isSystemCaptureDenial(denial) && isSystemCaptureDenial(multiple), "recognize direct and wrapped TCC denial")
        try check(!isSystemCaptureDenial(NSError(domain: SCStreamErrorDomain, code: SCStreamError.Code.failedToStart.rawValue)), "unrelated stream failures stay distinct")
        try checkMix()
        try checkMeetingAudioLevel()
        try checkMeetingInputRoute()
        try checkMeetingSystemAudioReadiness()
        try await checkMeetingSystemAudioSupervisor()
        try await checkMeetingSystemAudioPermissionDenial(multiple)
        try await checkMeetingSystemAudioOpenTimeout()
        try await checkMeetingSystemAudioConsoleRecovery()
        try checkMeetingMicrophoneSilence()
        try checkDictationReleaseTail()
        let packets: [MicrophonePacket] = [([0, 1, 2, 3], 0), ([4, 5], 4.0 / Double(Timeline.rate))]
        let queued = microphonePreRoll(packets, from: 2.5 / Double(Timeline.rate))
        try check(queued.flatMap { $0.samples } == [3, 4, 5], "warm pre-roll trims packets at the requested sample boundary")
        try check(queued.first?.time == 3.0 / Double(Timeline.rate), "the first trimmed packet keeps its new clock origin")
        try await checkColdMicrophoneOpen()
        try checkWarmClockRecovery()
        try checkWarmMicrophoneRetry()
        try await checkDictationInputRoute()
        try checkClockDrift()
        try checkConverter()
        try checkMicContinuity()
        try checkInputGap()
        try checkConvertedSync()
        try checkFile(directory: directory, seconds: CommandLine.arguments.contains("--long") ? 10800 : 3)
    }
}
