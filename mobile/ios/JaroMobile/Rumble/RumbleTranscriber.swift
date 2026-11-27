#if JARO_RUMBLE
import AVFAudio
import Foundation
import JaroRumble
import NaturalLanguage
import os
import UIKit

/// Runs the native runtime only in the containing app. The actor owns the cached model handle
/// and keeps it alive until any in-flight native call returns, including on timeout or cancellation.
actor RumbleTranscriber {
    struct Result: Equatable, Sendable {
        let text: String
        let language: String?
    }

    /// Wraps `OpaquePointer` for transfer between the actor and the detached inference task.
    /// Safety: `running` serializes all access to the handle, and `release()` defers closing
    /// until the active `work` task finishes.
    private struct HandleBox: @unchecked Sendable {
        let pointer: OpaquePointer
    }

    static let shared = RumbleTranscriber()
    nonisolated static let sampleRate = 16_000
    nonisolated static let maxFrames = 10 * sampleRate
    nonisolated static let loadGuardKey = "rumble2Flash"
    nonisolated static var fitsDevice: Bool { ProcessInfo.processInfo.physicalMemory >= 5 << 30 }
    // Budget beside the already prepared fallback model. The simulator reports zero.
    nonisolated static let minimumHeadroom = 1_200 << 20
    // Headroom required to allocate KV/encoder buffers when weights are already resident.
    nonisolated static let warmHeadroom = 700 << 20
    nonisolated static let idleReleaseDelay: Duration = .seconds(60)
    nonisolated static let decodeDeadline: Duration = .seconds(10)

    private nonisolated static let launchNoted: Void = {
        ParakeetLoadGuard(model: loadGuardKey).noteLaunch()
    }()

    nonisolated static func ensureLaunchNoted() {
        _ = launchNoted
    }

    nonisolated static func detectedLanguage(for transcript: String) -> String? {
        NLLanguageRecognizer.dominantLanguage(for: transcript)?.rawValue
    }

    nonisolated static func languageCode(forDetectedName name: String, transcript: String) -> String? {
        if let mapped = languageCodesByName[name.trimmingCharacters(in: .whitespacesAndNewlines)] {
            return mapped
        }
        return detectedLanguage(for: transcript)
    }

    private nonisolated static let languageCodesByName: [String: String] = [
        "Arabic": "ar",
        "Cantonese": "yue",
        "Chinese": "zh",
        "Czech": "cs",
        "Danish": "da",
        "Dutch": "nl",
        "English": "en",
        "Finnish": "fi",
        "French": "fr",
        "German": "de",
        "Greek": "el",
        "Hindi": "hi",
        "Hungarian": "hu",
        "Indonesian": "id",
        "Italian": "it",
        "Japanese": "ja",
        "Korean": "ko",
        "Malay": "ms",
        "Norwegian": "nb",
        "Norwegian Bokmål": "nb",
        "Norwegian Nynorsk": "nn",
        "Polish": "pl",
        "Portuguese": "pt",
        "Romanian": "ro",
        "Russian": "ru",
        "Slovak": "sk",
        "Spanish": "es",
        "Swedish": "sv",
        "Thai": "th",
        "Turkish": "tr",
        "Ukrainian": "uk",
        "Vietnamese": "vi",
    ]

    private var job: UInt64 = 0
    private var running = false
    private var cached: (path: String, handle: HandleBox)?
    private var releaseAfterRun = false
    private var idleTask: Task<Void, Never>?
    private var memoryWarningObserver: NSObjectProtocol?

    init() {
        memoryWarningObserver = NotificationCenter.default.addObserver(
            forName: UIApplication.didReceiveMemoryWarningNotification,
            object: nil,
            queue: nil
        ) { _ in
            Task { await RumbleTranscriber.shared.release() }
        }
    }

    func release() {
        idleTask?.cancel()
        idleTask = nil
        if running {
            releaseAfterRun = true
            return
        }
        closeCached()
    }

    private func closeCached() {
        guard let cached else { return }
        self.cached = nil
        jaro_rumble_close(cached.handle.pointer)
    }

    private func scheduleIdleRelease() {
        idleTask?.cancel()
        idleTask = Task {
            try? await Task.sleep(for: Self.idleReleaseDelay)
            guard !Task.isCancelled else { return }
            self.release()
        }
    }

    private static func hasHeadroom(_ minimum: Int) -> Bool {
        let available = os_proc_available_memory()
        #if targetEnvironment(simulator)
        return available == 0 || available >= minimum
        #else
        return available >= minimum
        #endif
    }

    func transcribeIfReady(_ audio: URL, duration: Double) async -> Result? {
        guard RumbleDictationSettings().routesToApp, RumbleModel.isDownloaded else { return nil }
        return await transcribeDetected(audio, duration: duration, model: RumbleModel.fileURL, removeCorrupt: true)
    }

    func transcribe(_ audio: URL, duration: Double, model: URL) async -> String? {
        await transcribeDetected(audio, duration: duration, model: model, removeCorrupt: false)?.text
    }

    func transcribeDetected(
        _ audio: URL,
        duration: Double,
        model: URL,
        removeCorrupt: Bool = false
    ) async -> Result? {
        Self.ensureLaunchNoted()
        guard Self.fitsDevice, duration > 0, duration <= 10, !running, !Task.isCancelled else { return nil }
        let loadGuard = ParakeetLoadGuard(model: Self.loadGuardKey)
        guard !loadGuard.lastLoadWasInterrupted else { return nil }

        let canReuse = cached?.path == model.path
        if canReuse {
            guard Self.hasHeadroom(Self.warmHeadroom) else {
                closeCached()
                return nil
            }
        } else {
            closeCached()
            guard Self.hasHeadroom(Self.minimumHeadroom) else { return nil }
        }

        idleTask?.cancel()
        idleTask = nil
        running = true
        releaseAfterRun = false
        job = job == UInt64.max ? 1 : job + 1
        let id = job
        let existingHandle = cached?.handle

        let work = Task.detached(priority: .userInitiated) { () -> (Result?, HandleBox?) in
            Self.recognize(
                audio,
                model: model,
                existingHandle: existingHandle,
                removeCorrupt: removeCorrupt,
                job: id
            )
        }
        let deadline = Task.detached {
            do {
                try await Task.sleep(for: Self.decodeDeadline)
                work.cancel()
                jaro_rumble_cancel(id)
            } catch {}
        }
        let (result, returnedHandle) = await withTaskCancellationHandler {
            await work.value
        } onCancel: {
            work.cancel()
            jaro_rumble_cancel(id)
        }
        deadline.cancel()

        if let returnedHandle {
            if releaseAfterRun {
                cached = nil
                jaro_rumble_close(returnedHandle.pointer)
            } else {
                cached = (path: model.path, handle: returnedHandle)
                scheduleIdleRelease()
            }
        } else {
            cached = nil
        }
        running = false
        releaseAfterRun = false
        return result
    }

    nonisolated private static func recognize(
        _ audio: URL,
        model: URL,
        existingHandle: HandleBox?,
        removeCorrupt: Bool,
        job: UInt64
    ) -> (Result?, HandleBox?) {
        guard !Task.isCancelled,
              let input = try? AVAudioFile(forReading: audio, commonFormat: .pcmFormatFloat32, interleaved: false),
              input.processingFormat.sampleRate == Double(sampleRate), input.processingFormat.channelCount == 1,
              input.length > 0, input.length <= AVAudioFramePosition(maxFrames),
              let buffer = AVAudioPCMBuffer(pcmFormat: input.processingFormat, frameCapacity: AVAudioFrameCount(input.length)),
              (try? input.read(into: buffer)) != nil,
              let samples = buffer.floatChannelData?.pointee, buffer.frameLength > 0 else {
            return (nil, existingHandle)
        }
        let loadGuard = ParakeetLoadGuard(model: loadGuardKey)
        guard !Task.isCancelled, !loadGuard.lastLoadWasInterrupted else { return (nil, existingHandle) }

        let handleBox: HandleBox
        if let existingHandle {
            handleBox = existingHandle
            loadGuard.begin()
        } else {
            guard let verified = RumbleModel.verifiedFileURL(at: model, removeCorrupt: removeCorrupt),
                  !Task.isCancelled else {
                return (nil, nil)
            }
            let threads = Int32(min(4, ProcessInfo.processInfo.activeProcessorCount))
            loadGuard.begin()
            guard let opened = verified.path.withCString({ jaro_rumble_open($0, threads) }) else {
                loadGuard.end(loaded: false)
                return (nil, nil)
            }
            handleBox = HandleBox(pointer: opened)
        }
        // Keep the interrupted-load marker until inference returns as it also allocates memory.
        defer { loadGuard.end(loaded: true) }

        guard !Task.isCancelled else { return (nil, handleBox) }
        var detectedBuffer = [CChar](repeating: 0, count: Int(JARO_RUMBLE_LANGUAGE_CAPACITY))
        let text = detectedBuffer.withUnsafeMutableBufferPointer { buf in
            jaro_rumble_transcribe_detected(handleBox.pointer, samples, Int32(buffer.frameLength), nil, job, buf.baseAddress)
        }
        guard let text else { return (nil, handleBox) }
        defer { jaro_rumble_free_text(text) }
        guard !Task.isCancelled, let decoded = String(validatingCString: text) else { return (nil, handleBox) }
        let output = decoded.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !output.isEmpty else { return (nil, handleBox) }
        let detectedName = String(validatingCString: detectedBuffer) ?? ""
        let language = languageCode(forDetectedName: detectedName, transcript: output)
        return (Result(text: output, language: language), handleBox)
    }
}
#endif
