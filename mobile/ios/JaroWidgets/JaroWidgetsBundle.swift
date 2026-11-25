import SwiftUI
import WidgetKit

@main
struct JaroWidgetsBundle: WidgetBundle {
    var body: some Widget {
        JaroDictationLiveActivity()
        if #available(iOS 18.0, *) {
            JaroDictationControl()
        }
    }
}
