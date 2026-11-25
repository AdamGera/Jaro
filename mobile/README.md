# Jaro mobile previews

Enable the Jaro keyboard in system settings and dictate into an editable field in another app. Both keyboards support swipe typing, autocorrect, vocabulary, and local speech recognition. They use separate mobile speech engines and store their settings on the phone or tablet.

| Platform | Requirements | Build |
| --- | --- | --- |
| iPhone and iPad | iOS or iPadOS 18+, with additional speech features on 26+ supported devices | [Xcode project, App Group, and signing](ios/README.md) |
| Android | Android 12+ on ARM64 | [Gradle project, offline models, and signing](android/README.md) |

## Release status

- **iOS and iPadOS** built a complete unsigned Release archive with the app, keyboard, and widgets. Distribution requires Jaro’s Apple Developer signing and App Group provisioning. Verify keyboard setup, microphone and Full Access prompts, model downloads, offline insertion, and screen-lock recovery on physical devices.
- **Android** built a release APK, a Play bundle, and a separate Preview APK with disposable signing keys. Preview checks for APK updates and opens the download in a browser. Verify physical microphone insertion, TalkBack, and the production signing path before distributing.

The platform guides record the checks completed so far. Desktop recordings, meeting notes, and Rumble2 Flash are part of the desktop app. Mobile Rumble2 Flash integration is tracked separately.

[Mobile privacy policy](PRIVACY.md)
