import Foundation

#if JARO_RUMBLE || JARO_RUMBLE_HANDOFF
/// The app publishes readiness only after the pinned model has been verified and installed.
/// The keyboard uses these flags to request host dictation without loading the native runtime.
nonisolated struct RumbleDictationSettings {
    private let defaults: UserDefaults?

    init(defaults: UserDefaults? = UserDefaults(suiteName: KeyboardBridge.appGroup)) {
        self.defaults = defaults
    }

    var isEnabled: Bool {
        get {
            guard let defaults, defaults.object(forKey: "experiments.rumble2Flash") != nil else { return true }
            return defaults.bool(forKey: "experiments.rumble2Flash")
        }
        nonmutating set { defaults?.set(newValue, forKey: "experiments.rumble2Flash") }
    }

    var isModelReady: Bool {
        get { defaults?.bool(forKey: "experiments.rumble2Flash.ready") ?? false }
        nonmutating set { defaults?.set(newValue, forKey: "experiments.rumble2Flash.ready") }
    }

    var routesToApp: Bool { isEnabled && isModelReady }
}
#endif
