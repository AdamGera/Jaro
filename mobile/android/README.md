# Jaro for Android

Jaro is a native Android keyboard with on-device dictation. It runs on ARM64 phones and tablets with Android 12 or later.

Open Jaro, grant microphone access, then enable and select the Jaro keyboard in Android Settings. Open an editable field and hold the Jaro key to speak. Release to insert the transcript. You can also tap the key to start and tap the check mark to finish. In Jaro itself, use **Dictate** to record text you can copy.

- Typing includes swipe gestures, autocorrection, suggestions, accents, symbol and emoji panels, and shortcuts from the Android personal dictionary. The keyboard has English, Spanish, German, Norwegian Bokmål, French, Italian, Portuguese, and Russian layouts.
- Dictation also supports Dutch, Polish, and Russian. Choose a language or use Automatic, which follows the selected keyboard layout or the phone language. Choose Polished, Casual, Notes, or Verbatim. Dictation settings also include custom vocabulary, spoken punctuation, and list formatting. Under **Dictation → Words**, add replacements or import and export vocabulary and replacements as a `.jarowords` file.
- Hide the keyboard during dictation to finish and enter the result in the same field. Moving to another field discards unfinished dictation.
- In **Labs**, enable **Dictate from Quick Settings** to add a tile that opens a recording panel and copies the result. Labs also includes an optional signature, number row and emoji suggestions.
- The bundled Whisper Tiny model works offline after installation. Choose **Download** in Dictation settings for the optional 190 MB Whisper Small model. The build verifies the bundled model, and the app verifies the optional model against a pinned SHA-256 hash. Under **Labs → Faster English dictation**, download the optional 135 MB Parakeet model for English. Whisper remains the default and handles other languages.

In the standard build, **Check for updates** on Home opens the Jaro Releases page in a browser. The sideloaded Preview build can also check and display its own updates inside the app. **Send feedback** opens the Jaro issue tracker.

The in-app Privacy page describes microphone use, keyboard data, and the optional model download. The app also includes the speech, typing-model, font, and source licenses.

## Build from source

Install JDK 17, Android SDK Platform 36, Android NDK 27.1.12297006, and CMake 3.22.1. Set `ANDROID_HOME` to the SDK directory. Run these commands from this directory.

```sh
./gradlew :app:testDebugUnitTest :app:lintDebug :app:assembleDebug
```

The build fetches the pinned Whisper Tiny weights, whisper.cpp, and transcribe.cpp when needed. The optional models are downloaded in the app. The project bundles the platform-independent typing data. The debug APK is under `app/build/outputs/apk/debug/`.

### Rumble2 Flash build

The standard build includes Whisper and supports the optional English-only Parakeet model. To include Rumble2 Flash, build the shared Android ARM64 library from the mobile runtime using the Android NDK. Set `JARO_ANDROID_RUMBLE_LIBRARY` to the resulting `libjaro_rumble.so` and `JARO_ANDROID_RUMBLE_SHA256` to its SHA-256 before running Gradle. The build requires 16 KiB ELF load segments and bundles the shared runtime notices. [Mobile runtime](../rumble/)

This build enables Rumble2 Flash by default. Open **Settings → Dictation → Rumble2 Flash** to download the 445 MB model or switch models. Rumble2 Flash handles up to 10 seconds per dictation. With **Automatic** selected, it detects the spoken language and applies Jaro’s available formatting for that language. Jaro uses its standard speech engine for longer dictation, translation, or when Rumble2 Flash is unavailable.

## Release artifacts

Use a Jaro Android signing key. Point `JARO_ANDROID_KEYSTORE_PROPERTIES` to a private properties file containing `storeFile`, `storePassword`, `keyAlias`, and `keyPassword`. The keystore path is relative to that file. Keep the key and file outside the repository. Increment `versionCode` for each release and set `versionName` in `app/build.gradle.kts` to the intended Jaro release.

```sh
./gradlew :app:testReleaseUnitTest :app:lintRelease :app:assembleRelease :app:bundleRelease
```

Publish the signed APK for direct installation and use the AAB for Google Play. Choose the signing setup before the first release. If users should be able to upgrade between GitHub and Google Play, use the same distribution signing certificate for both channels. [Android app signing guide](https://developer.android.com/studio/publish/app-signing)

Before publishing, exercise first launch, microphone denial and recovery, keyboard selection, real speech insertion into another app with the network off, swipe typing, TalkBack, and a signed upgrade using the release key. Verify the released artifact's signing certificate and record its SHA-256. The source stores no publisher credentials.

## Preview verification

Debug and release builds passed 163 unit tests each and Android lint. The release APK and AAB built with a disposable test key. On ARM64 Android 16.1, an offline upgrade from build 1 to build 2 preserved the selected keyboard, microphone grant, and number row setting. The number row typed into a separate app before and after the upgrade. The setup flow recovered when microphone access was denied and then granted. The configured Rumble2 Flash APK passed offline Spanish and French tests using both automatic and explicit language selection. The regular engine handled longer recordings and low memory. Physical microphone insertion and TalkBack are pending.

## Sideloaded Preview builds

Jaro Preview installs separately as `ai.jard.jaro.preview`. Give it a dedicated private signing key. Set `JARO_ANDROID_PREVIEW_KEYSTORE_PROPERTIES` to its properties file, using the same fields as the release key. Keep using that key for each Preview update. Set `jaroPreviewBuild` to an increasing integer and keep `versionName` in `app/build.gradle.kts` at the intended Jaro version.

```sh
./gradlew :app:testPreviewUnitTest :app:lintPreview :app:assemblePreview -PjaroPreviewBuild=12
```

Preview adds **Check now**, a release card, and a dismissible keyboard notice. Automatic checks run on opening Preview and at most once a day from a non-password keyboard field. Turn them off on Home. **Download update** opens the exact Preview APK in the browser. Android handles confirmation and verifies that updates match the installed signing key. The Preview app itself requests no APK installation permission.

The Preview channel reads `latest.json` from the fixed `android-preview-channel` prerelease in the Jaro repository. The channel manifest can point to a new Android Preview release once its APK is available. Publish each immutable APK on the corresponding `android-preview-N` release, with an asset named `JaroPreview-V-preview.N.apk` for version `V` and build `N`. Keep the channel release marked as prerelease and separate from the ordinary releases. For example, a build with version `0.0.9` and build code `12` uses the following manifest shape. Use the actual APK size and SHA-256.

```json
{
  "versionCode": 12,
  "versionName": "0.0.9-preview.12",
  "version": "0.0.9",
  "minSdk": 31,
  "published": "2026-11-24",
  "notes": ["Example release note"],
  "apk": {
    "url": "https://github.com/AdamGera/Jaro/releases/download/android-preview-12/JaroPreview-0.0.9-preview.12.apk",
    "size": 65000000,
    "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
}
```

A signed QA build on an isolated ARM64 Android 16.1 emulator verified that **Check now** reports a failed check in airplane mode while Dictate stays available. Turning off automatic checks persisted through a cold relaunch. The Preview channel needs its own published manifest and signed APKs before end-to-end update checks.
