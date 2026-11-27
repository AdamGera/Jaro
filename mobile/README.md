# Jaro mobile previews

Enable the Jaro keyboard in system settings and dictate into an editable field in another app. Both keyboards support swipe typing, autocorrect, vocabulary, word replacements, and local speech recognition. You can move vocabulary and replacements between the apps with a `.jarowords` file.

| Platform | Requirements | Build |
| --- | --- | --- |
| iPhone and iPad | iOS or iPadOS 18+, with additional speech features on 26+ supported devices | [Xcode project, App Group, and signing](ios/README.md) |
| Android | Android 12+ on ARM64 | [Gradle project, offline models, and signing](android/README.md) |

## Release status

- **iOS and iPadOS** built a complete unsigned Release archive with the app, keyboard, and widgets. Distribution requires Jaro’s Apple Developer signing and App Group provisioning. Verify keyboard setup, microphone and Full Access prompts, model downloads, offline insertion, and screen-lock recovery on physical devices.
- **Android** built a release APK, a Play bundle, and a separate Preview APK with disposable signing keys. Preview checks for APK updates and opens the download in a browser. Verify physical microphone insertion, TalkBack, and the production signing path before distributing.

## Rumble2 Flash builds

Mobile builds that include the Rumble2 Flash runtime select it by default for short multilingual dictation. The app downloads 445 MB of verified model weights. Once installed, it runs offline. Use **Settings → Dictation** to switch back to the standard speech engine. Jaro also uses that engine for recordings over ten seconds and when Rumble2 Flash cannot run.

On iPhone and iPad, the keyboard hands dictation to the Jaro app, which runs the model. Its download starts on Wi-Fi, with a **Use mobile data** option. Follow the build instructions for [iOS and iPadOS](ios/README.md#rumble2-flash-build) or [Android](android/README.md#rumble2-flash-build). The [shared runtime guide](rumble/README.md) documents the pinned build.

Spanish and French passed offline tests in the iOS Simulator and Android ARM64 emulator. Physical-device validation is pending.

[Mobile privacy policy](PRIVACY.md)
