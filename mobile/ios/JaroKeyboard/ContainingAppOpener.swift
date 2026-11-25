import UIKit

/// Uses the automatic keyboard handoff only in local Debug builds. iOS does not offer
/// a documented containing-app opener for custom keyboards. Release builds use the
/// manual instruction in KeyboardViewController.
@MainActor
enum ContainingAppOpener {
    #if DEBUG
    private typealias OpenFunction = @convention(c) (
        AnyObject, Selector, NSURL, NSDictionary, AnyObject?
    ) -> Void

    @discardableResult
    static func open(_ url: URL, from responder: UIResponder) -> Bool {
        let selector = NSSelectorFromString("openURL:options:completionHandler:")
        guard let application = application(from: responder),
              application.responds(to: selector) else {
            return false
        }
        let implementation = application.method(for: selector)
        let open = unsafeBitCast(implementation, to: OpenFunction.self)
        open(application, selector, url as NSURL, NSDictionary(), nil)
        return true
    }

    private static func application(from responder: UIResponder) -> UIResponder? {
        var current: UIResponder? = responder
        while let candidate = current {
            if candidate.isKind(of: UIApplication.self) {
                return candidate
            }
            current = candidate.next
        }
        return (UIApplication.self as AnyObject).value(forKey: "sharedApplication") as? UIResponder
    }
    #else
    static func open(_ url: URL, from responder: UIResponder) -> Bool {
        false
    }
    #endif
}
