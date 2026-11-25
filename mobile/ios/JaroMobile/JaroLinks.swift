import SafariServices
import UIKit

enum JaroLinks {
    static let privacyPolicy = URL(string: "https://github.com/AdamGera/Jaro/blob/main/mobile/PRIVACY.md")!
    static let support = URL(string: "https://github.com/AdamGera/Jaro/issues")!
}

/// Opens a web page in Safari inside Jaro (`SFSafariViewController`), over whatever is showing.
@MainActor
enum SafariSheet {
    static func present(_ url: URL) {
        let scene = UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }
            .first { $0.activationState == .foregroundActive }
        guard var top = scene?.keyWindow?.rootViewController else { return }
        while let presented = top.presentedViewController, !presented.isBeingDismissed {
            top = presented
        }
        let controller = SFSafariViewController(url: url)
        controller.overrideUserInterfaceStyle = .dark
        top.present(controller, animated: true)
    }
}
