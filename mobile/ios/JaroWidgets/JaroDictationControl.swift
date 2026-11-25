import AppIntents
import SwiftUI
import WidgetKit

@available(iOS 18.0, *)
struct JaroDictationControl: ControlWidget {
    static let kind = "ai.jard.jaro.ios.control.dictate"

    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: Self.kind) {
            ControlWidgetButton(action: ToggleHexControlIntent()) {
                Label("Dictate", systemImage: "mic.fill")
                Text("Jaro")
            }
        }
        .displayName("Dictate with Jaro")
        .description("Starts or finishes on-device dictation and copies the text.")
    }
}
