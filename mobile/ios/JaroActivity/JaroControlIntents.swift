import AppIntents
import Foundation

/// Shared between JaroMobile and JaroWidgets so Control Center, Lock Screen, and Action Button
/// controls can start or finish dictation in Jaro.
@available(iOS 18.0, *)
nonisolated struct ToggleHexControlIntent: AppIntent {
    static let title: LocalizedStringResource = "Dictate with Jaro"
    static let description = IntentDescription("Starts or finishes on-device dictation and copies the text.")
    @available(iOS 26.0, *)
    static let supportedModes: IntentModes = .foreground
    static let isDiscoverable = false

    init() {}

    func perform() async throws -> some IntentResult {
        await JaroSystemCommandBus.shared.dispatch(.toggleDictation)
        return .result()
    }
}
