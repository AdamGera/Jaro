# Jaro for iPhone and iPad

Jaro includes a SwiftUI dictation app, a system keyboard, and extensions for Live Activity and Control Center. The keyboard includes swipe typing, autocorrect, layouts, and emoji. The app includes setup, language and vocabulary controls, and dictation styles. Live words, in-keyboard dictation, and other experiments depend on the OS, language, and device.

Speech uses Apple's on-device recognizer or the downloadable FluidAudio Parakeet models. The optional model-based cleanup component requires the corresponding Core ML artifact. For local Debug testing, run `scripts/fetch-cleanup-model.sh` before generating the project. Release archives use rules-based cleanup and exclude the trained artifact pending redistribution terms. Available speech and keyboard language combinations are shown in the app.

## Build

Use Xcode 26.5 or later with the iOS platform, an iOS Simulator runtime, and the Metal Toolchain installed. Install XcodeGen 2.46.0 or later. From this directory, generate the project and build for a simulator.

```sh
xcodegen generate
xcodebuild -project JaroMobile.xcodeproj -scheme JaroMobile \
  -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO build
```

With a simulator booted, run the test scheme by passing the simulator ID reported by `xcrun simctl list devices booted` to `xcodebuild test -destination 'id=DEVICE_ID'`. Use separate simulator data for testing.

An unsigned Release archive built on Xcode 26.5 with the app, keyboard, Live Activity and Control Center extension, compiled shaders, icon catalog, and license and privacy files. The archive excludes the trained cleanup model and the Debug-only keyboard app opener. Device signing and physical-device checks remain.

On the iOS 26.5 iPhone 17 Pro Simulator, 970 tests passed and four tests requiring optional speech or typing fixtures were skipped. A host-app preview of the full keyboard also rendered on the iPad Pro Simulator.

## Device and release

Configure the containing app `ai.jard.jaro.ios`, keyboard `ai.jard.jaro.ios.keyboard`, and widgets `ai.jard.jaro.ios.widgets` under Jaro's Apple Developer team. The app and keyboard use `group.ai.jard.jaro.ios`. Register that group and enable it on both targets' profiles. Select Jaro's development team in Xcode for a device build or supply `DEVELOPMENT_TEAM` when archiving.

Before distributing, test first run, Full Access, dictation in a separate app, screen-lock recovery, model download, offline transcription, and the iPad layout on physical devices. Set the release version and increment the build number in `project.yml`. The containing app and both extensions use those shared settings. Regenerate the project, create an archive with Jaro’s team, and validate it in Xcode Organizer before sending it to TestFlight.

```sh
xcodebuild -project JaroMobile.xcodeproj -scheme JaroMobile \
  -configuration Release -destination 'generic/platform=iOS' \
  -archivePath build/Jaro.xcarchive DEVELOPMENT_TEAM=YOUR_TEAM_ID archive
```

Release builds tell you to open Jaro from the Home Screen when the keyboard needs the containing app. For a keyboard dictation session, choose a duration under **Settings → Dictation → Keep mic warm**, then return to the typing app. Local Debug builds also exercise automatic handoff through UIKit. Verify the manual path with and without Full Access before submitting the app. [Apple keyboard extension guide](https://developer.apple.com/documentation/uikit/configuring-open-access-for-a-custom-keyboard) · [App Review Guidelines](https://developer.apple.com/app-store/review/guidelines/)

For App Review, use Jaro's own app icon, branding, screenshots, and product description. Review the app against guideline 4.1 before submission. Exercise ordinary typing and switching to the next keyboard with Full Access and network access both off, as required by guideline 4.4.1. Include the steps for microphone permissions and optional model setup in the review notes. [Apple App Review Guidelines](https://developer.apple.com/app-store/review/guidelines/)

[Source license](LICENSE) · [Data and model notices](THIRD_PARTY_NOTICES.md) · [Privacy policy](../PRIVACY.md)
