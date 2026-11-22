import Foundation
import Darwin

@main enum CaptureCLI {
    static func main() async {
        let args = Array(CommandLine.arguments.dropFirst())
        if args == ["devices"] {
            let inputs = InputDevice.available()
            let systemDefault = InputDevice.current()
            let lidClosed = MicrophoneLid.closed
            let dictationMicrophones = [false, true].map { avoidBluetooth in
                let route = DictationInputRoute.resolve(savedUID: nil, available: inputs, systemDefault: systemDefault,
                                                        lidClosed: lidClosed, avoidBluetooth: avoidBluetooth)
                return route.system.id == 0 ? "" : route.system.name
            }
            let listedInputs = inputs.map { device -> [String: Any] in
                var input: [String: Any] = ["id": device.uid, "name": device.name]
                if lidClosed && device.isInternalMicrophone { input["unavailable"] = true }
                return input
            }
            emit(["event": "devices", "microphone": systemDefault.name, "dictationMicrophones": dictationMicrophones,
                  "inputs": listedInputs])
            return
        }
        let usage = "Use devices, warm [--on-demand], or record --mode both|mic|system --output /absolute/file.wav [--separate-tracks [--call-application bundle-id] | --meter [--cold-recovery]] [--input device-uid] [--avoid-bluetooth]"
        let warm = args.first == "warm"
        guard warm || (args.count >= 5 && args[0] == "record" && args[1] == "--mode" &&
              CaptureMode(rawValue: args[2]) != nil && args[3] == "--output" && args[4].hasPrefix("/")) else {
            emit(["event": "error", "message": usage])
            exit(2)
        }
        var options = Array(args.dropFirst(warm ? 1 : 5))
        let onDemand = warm && options.first == "--on-demand"
        if onDemand { options.removeFirst() }
        let retainSources = !warm && args[2] == "both" && options.first == "--separate-tracks"
        let detailedMeter = !warm && args[2] == "mic" && options.first == "--meter"
        if retainSources || detailedMeter { options.removeFirst() }
        var callApplication: String?
        if retainSources, options.first == "--call-application", options.count >= 2, !options[1].isEmpty {
            callApplication = options[1]
            options.removeFirst(2)
        }
        var meetingSystemMode = MeetingSystemAudioMode.screen
        var hostPID: pid_t?
        if retainSources, options.first == "--system-audio", options.count >= 4,
           let backend = MeetingSystemAudioMode(rawValue: options[1]), options[2] == "--host-pid",
           let pid = pid_t(options[3]), pid > 1 {
            meetingSystemMode = backend
            hostPID = pid
            options.removeFirst(4)
        }
        let recoverCold = detailedMeter && options.first == "--cold-recovery"
        if recoverCold { options.removeFirst() }
        let avoidBluetooth = options == ["--avoid-bluetooth"] || (options.count == 3 && options.last == "--avoid-bluetooth")
        if avoidBluetooth { options.removeLast() }
        let inputUID: String?
        if options.isEmpty {
            inputUID = nil
        } else if options.count == 2, options[0] == "--input", !options[1].isEmpty {
            inputUID = options[1]
        } else {
            emit(["event": "error", "message": usage])
            exit(2)
        }
        signal(SIGPIPE, SIG_IGN)
        if warm {
            await runWarmMicrophone(uid: inputUID, avoidBluetooth: avoidBluetooth, onDemand: onDemand)
            return
        }
        let mode = CaptureMode(rawValue: args[2])!
        let (events, stop) = AsyncStream<String>.makeStream(bufferingPolicy: .bufferingOldest(1))
        let recorder = Recorder(mode: mode, path: args[4], stopReason: stop, inputUID: inputUID,
                                avoidBluetooth: avoidBluetooth, retainSources: retainSources,
                                detailedMeter: detailedMeter, recoverColdOpen: recoverCold, callApplication: callApplication,
                                meetingSystemMode: meetingSystemMode, hostPID: hostPID)
        // Recorder commands use the mix queue.
        nonisolated(unsafe) let commands = recorder
        let signals = [SIGTERM, SIGINT].map { number -> DispatchSourceSignal in
            signal(number, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: number, queue: .global())
            source.setEventHandler { commands.requestStop() }
            source.resume()
            return source
        }
        DispatchQueue.global().async {
            while let command = readLine() {
                if command == "stop" { commands.requestStop(); return }
                if let release = dictationReleaseTime(command) {
                    if detailedMeter { commands.requestFinish(releasedAt: release) }
                    else { commands.requestStop(); return }
                }
                if command.hasPrefix("copy "), let request = UUID(uuidString: String(command.dropFirst(5))) {
                    commands.markMeetingCopy(request)
                }
                if retainSources && command.hasPrefix("input "),
                   let uid = try? JSONDecoder().decode(String.self, from: Data(command.dropFirst(6).utf8)),
                   !uid.contains("\0") {
                    commands.selectMeetingInput(uid.isEmpty ? nil : uid)
                }
                if callApplication != nil {
                    let fields = command.split(separator: " ")
                    if fields.count == 2, fields[0] == "recover-mic" || fields[0] == "defer-mic",
                       let id = UUID(uuidString: String(fields[1])) {
                        commands.respondToMeetingMicRecovery(id, allowed: fields[0] == "recover-mic")
                    }
                }
            }
            commands.requestStop("Recorder connection closed")
        }
        do {
            #if JARD_CAPTURE_FIXTURE
            try startSyntheticInput(recorder)
            #else
            try await recorder.start()
            #endif
            var iterator = events.makeAsyncIterator()
            let reason = await iterator.next() ?? "Stopped"
            try await recorder.finish(reason: reason)
        } catch {
            try? await recorder.finish(reason: error.localizedDescription)
            var failure: [String: Any] = ["event": "error", "message": error.localizedDescription]
            if let permission = error as? RecordingPermissionError {
                failure["permission"] = permission.source.rawValue
            } else if detailedMeter, let microphone = error as? MicrophoneOpenError {
                failure["interruption"] = "microphone"
                failure["failedAt"] = microphone.failedAt
            }
            emit(failure)
            exit(1)
        }
        signals.forEach { $0.cancel() }
    }
}
