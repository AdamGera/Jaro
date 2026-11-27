#if JARO_RUMBLE
import Foundation
import Observation
import SwiftUI

/// Downloads beside the selected speech model. The existing downloader resumes partial files
/// and verifies the pinned digest before publishing the model to the host and keyboard.
@MainActor
@Observable
final class RumbleModel {
    static let shared = RumbleModel()

    nonisolated static let file = ParakeetUltraModel.File(
        path: "rumble-2-flash-q4_k.gguf",
        size: 444_897_792,
        sha256: "0e4c0169bfd99973e501b1ecaab946e5589ada4fc110b458a4c029423759cdb2"
    )
    nonisolated static let baseURL = "https://huggingface.co/cstr/rumble-2-flash-GGUF/resolve/780b126551f62603755e534952ad2942ac0ebcef"
    nonisolated static var directory: URL {
        SpeechModelStore.root.appendingPathComponent("rumble-2-flash-780b126", isDirectory: true)
    }
    nonisolated static var fileURL: URL { directory.appendingPathComponent(file.path) }
    nonisolated static var isDownloaded: Bool {
        SpeechModelStore.isComplete(directory) && ParakeetUltraModel.isDownloaded(in: directory, files: [file])
    }

    private nonisolated struct VerifiedRecord: Equatable {
        let path: String
        let modified: Date?
        let size: Int64
    }

    private nonisolated static let verificationLock = NSLock()
    private nonisolated(unsafe) static var verifiedRecord: VerifiedRecord?

    /// Verifies the pinned SHA-256 digest once per process and whenever the file changes on disk.
    /// Corrupt installed weights are removed and readiness is cleared so the keyboard falls back cleanly.
    nonisolated static func verifiedFileURL(
        at url: URL = fileURL,
        removeCorrupt: Bool = true,
        defaults: UserDefaults? = UserDefaults(suiteName: KeyboardBridge.appGroup)
    ) -> URL? {
        let attributes = try? FileManager.default.attributesOfItem(atPath: url.path)
        guard let size = (attributes?[.size] as? NSNumber)?.int64Value, size == file.size else {
            return nil
        }
        let modified = attributes?[.modificationDate] as? Date
        let record = VerifiedRecord(path: url.path, modified: modified, size: size)
        if verificationLock.withLock({ verifiedRecord == record }) {
            return url
        }
        if (try? ParakeetUltraModel.sha256(of: url)) == file.sha256 {
            verificationLock.withLock { verifiedRecord = record }
            return url
        }
        verificationLock.withLock {
            if verifiedRecord?.path == url.path { verifiedRecord = nil }
        }
        if removeCorrupt {
            try? FileManager.default.removeItem(at: url)
            SpeechModelStore.clearComplete(url.deletingLastPathComponent())
            RumbleDictationSettings(defaults: defaults).isModelReady = false
            if url.standardizedFileURL == fileURL.standardizedFileURL {
                Task { @MainActor in
                    RumbleModel.shared.noteCorruptModel()
                }
            }
        }
        return nil
    }

    let download = ParakeetUltraDownload(
        directory: RumbleModel.directory,
        files: [RumbleModel.file],
        base: RumbleModel.baseURL,
        modelLabel: "Rumble2 Flash",
        mayDownload: { SpeechModelDownloadNetwork.shared.mayDownload(.rumble) },
        isWanted: { RumbleDictationSettings().isEnabled && RumbleTranscriber.fitsDevice }
    )
    private let settings = RumbleDictationSettings()
    @ObservationIgnored private var stopping: Task<Void, Never>?
    private(set) var isEnabled = true
    private(set) var isReady = false

    private init() {
        RumbleTranscriber.ensureLaunchNoted()
        isEnabled = settings.isEnabled
        let modelDownload = download
        download.onDownloaded = { [weak self] in
            SpeechModelDownloadNetwork.shared.forgetMobileData(for: .rumble)
            self?.refreshReadiness()
            self?.download.settle()
        }
        SpeechModelDownloadNetwork.shared.watch()
        SpeechModelDownloadNetwork.shared.observe { [weak modelDownload] _ in
            modelDownload?.connectionChanged()
        }
        refreshReadiness()
        startIfNeeded()
    }

    private func refreshReadiness() {
        let wasReady = isReady
        isReady = isEnabled && RumbleTranscriber.fitsDevice && Self.isDownloaded
            && !ParakeetLoadGuard(model: RumbleTranscriber.loadGuardKey).lastLoadWasInterrupted
        settings.isModelReady = isReady
        if isReady != wasReady {
            DictationController.shared.refreshWarmDuration()
        }
    }

    func noteCorruptModel() {
        refreshReadiness()
        guard isEnabled else { return }
        download.fail(String(localized: "Rumble2 Flash weights were damaged and removed. Tap Try again to re-download."))
    }

    func setEnabled(_ enabled: Bool) {
        isEnabled = enabled
        settings.isEnabled = enabled
        refreshReadiness()
        if enabled {
            startIfNeeded()
        } else {
            SpeechModelDownloadNetwork.shared.forgetMobileData(for: .rumble)
            Task { await RumbleTranscriber.shared.release() }
            if stopping == nil {
                // Keep the completed file or resumable partial; disabling stops network traffic.
                stopping = Task { [weak self] in
                    guard let self else { return }
                    await self.download.stop()
                    self.stopping = nil
                    self.startIfNeeded()
                }
            }
        }
    }

    private func startIfNeeded() {
        guard isEnabled, RumbleTranscriber.fitsDevice, stopping == nil, case .idle = download.state else { return }
        if Self.isDownloaded {
            if !isReady {
                download.fail(String(localized: "Rumble2 Flash closed while loading. Jaro is using its usual model."))
            }
            return
        }
        download.start()
    }

    func appBecameActive() {
        refreshReadiness()
        guard isEnabled, stopping == nil else { return }
        download.appBecameActive()
        startIfNeeded()
    }

    func appMovedToBackground() {
        guard isEnabled else { return }
        download.appMovedToBackground()
    }

    func useMobileData() {
        SpeechModelDownloadNetwork.shared.allowMobileData(for: .rumble)
        download.connectionChanged()
    }

    func retry() {
        guard isEnabled, !isReady, stopping == nil else { return }
        if Self.isDownloaded {
            ParakeetLoadGuard(model: RumbleTranscriber.loadGuardKey).forget()
            refreshReadiness()
            download.settle()
            return
        }
        download.restart()
    }
}

struct RumbleExperimentRows: View {
    private let model = RumbleModel.shared

    var body: some View {
        SettingsToggleRow(
            title: String(localized: "Rumble2 Flash"),
            detail: RumbleTranscriber.fitsDevice
                ? String(localized: "On-device multilingual dictation for short recordings. 445 MB download. To use it from the keyboard, open Jaro and turn on Keep mic warm.")
                : String(localized: "Rumble2 Flash needs a device with at least 6 GB of memory."),
            isOn: Binding(get: { model.isEnabled }, set: { model.setEnabled($0) })
        )
        .disabled(!RumbleTranscriber.fitsDevice)

        if model.isEnabled, model.download.status != nil {
            MultilingualModelStatus(
                download: model.download,
                retry: model.retry,
                turnOff: { model.setEnabled(false) },
                useMobileData: model.useMobileData,
                accessibilityPrefix: "rumble-model"
            )
                .padding(.horizontal, 12)
                .padding(.bottom, 12)
        } else if model.isReady {
            Text("Downloaded for short dictations")
                .font(.system(size: 13))
                .foregroundStyle(JaroTheme.textMuted)
                .padding(.horizontal, 16)
                .padding(.bottom, 12)
        }
        SettingsDivider()
    }
}
#endif
