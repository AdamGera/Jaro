import OSLog
import SwiftUI
import UIKit

/// Reviews a words file from Files or AirDrop before adding its entries.
/// Only the copy that iOS placed in Jaro's inbox is removed after review.
@MainActor
enum JaroWordsInbox {
    static let fileExtension = "jarowords"
    private static let log = Logger(subsystem: "ai.jard.jaro.ios", category: "inbox")

    /// Where iOS puts the copies it hands Jaro.
    private static var inboxFolders: [URL] {
        var folders: [URL] = []
        if let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first {
            folders.append(documents.appendingPathComponent("Inbox", isDirectory: true))
        }
        if let bundle = Bundle.main.bundleIdentifier {
            folders.append(FileManager.default.temporaryDirectory.appendingPathComponent(bundle + "-Inbox", isDirectory: true))
        }
        return folders
    }

    /// Handles a file URL Jaro was asked to open; false for any other URL.
    @discardableResult
    static func open(_ url: URL) -> Bool {
        guard url.isFileURL, url.pathExtension.lowercased() == fileExtension else { return false }
        review(url)
        return true
    }

    static func review(_ url: URL, removeInboxCopy: Bool = true) {
        let removesCopy = removeInboxCopy && isInInbox(url) && url.pathExtension.lowercased() == fileExtension
        Task {
            let result = await Task.detached(priority: .userInitiated) {
                let scoped = url.startAccessingSecurityScopedResource()
                defer { if scoped { url.stopAccessingSecurityScopedResource() } }
                return JaroWordsReader.read(contentsOf: url)
            }.value
            logResult(result)
            present(result) {
                if removesCopy { try? FileManager.default.removeItem(at: url) }
            }
        }
    }

    /// Clears copies a sheet never got to delete (Jaro ended while it showed). Inbox copies
    /// are cleared only once they are an hour old, so a file that is arriving now stays.
    static func sweep(now: Date = Date()) {
        for inbox in inboxFolders {
            guard let files = try? FileManager.default.contentsOfDirectory(
                at: inbox, includingPropertiesForKeys: [.contentModificationDateKey]
            ) else { continue }
            for file in files where file.pathExtension.lowercased() == fileExtension {
                guard let modified = (try? file.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate,
                      now.timeIntervalSince(modified) >= 3600 else { continue }
                try? FileManager.default.removeItem(at: file)
            }
        }
    }

    private static func isInInbox(_ url: URL) -> Bool {
        let path = url.standardizedFileURL.resolvingSymlinksInPath().path
        return inboxFolders.contains { path.hasPrefix($0.standardizedFileURL.resolvingSymlinksInPath().path + "/") }
    }

    private static func logResult(_ result: JaroWordsReadResult?) {
        switch result {
        case nil:
            log.notice("words file couldn't be opened")
        case .refused(let reason):
            log.notice("words file refused: \(reason.rawValue, privacy: .public)")
        case .read(let file):
            let reasons = Dictionary(grouping: file.skipped, by: \.reason.rawValue)
                .map { "\($0.key) \($0.value.count)" }.sorted().joined(separator: ", ")
            log.notice("words file read: version \(file.version), \(file.vocabulary.count) terms, \(file.replacements.count) replacements, skipped: \(reasons, privacy: .public)")
        }
    }

    private static func present(_ result: JaroWordsReadResult?, onClose: @escaping () -> Void) {
        let scene = UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }
            .first { $0.activationState == .foregroundActive }
            ?? UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first
        guard var top = scene?.keyWindow?.rootViewController ?? scene?.windows.first?.rootViewController else {
            onClose()
            return
        }
        while let presented = top.presentedViewController, !presented.isBeingDismissed {
            top = presented
        }
        let controller = UIHostingController(rootView: JaroWordsReviewSheet(result: result, onClose: onClose))
        controller.overrideUserInterfaceStyle = .dark
        top.present(controller, animated: true)
    }
}
