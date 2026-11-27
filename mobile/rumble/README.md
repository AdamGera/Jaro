# Rumble2 Flash for Jaro mobile

This directory builds the optional CPU runtime for local multilingual transcription. Both mobile apps use the same C interface and the same pinned [Q4_K model](https://huggingface.co/cstr/rumble-2-flash-GGUF/tree/780b126551f62603755e534952ad2942ac0ebcef). The 445 MB weights are downloaded by the app when requested. The native runtime has no network or microphone access.

## Build

Use Python 3.8+, CMake 3.22+, and Ninja. The iOS build needs Xcode. The Android build needs NDK r27 or newer. You can pass explicit `--cmake` and `--ninja` paths if they were installed under the Android SDK.

```sh
python3 mobile/rumble/build.py --target ios --output /tmp/jaro-rumble
python3 mobile/rumble/build.py --target android --output /tmp/jaro-rumble --ndk /path/to/ndk
```

- iOS writes `/tmp/jaro-rumble/ios/JaroRumble.xcframework` with ARM64 device and Simulator slices. The Jaro host app links the static library and Accelerate. `--simulator-arch x86_64` builds the Simulator slice for an Intel Mac.
- Android writes `/tmp/jaro-rumble/android/arm64-v8a/libjaro_rumble.so`. It links ggml and C++ statically and verifies that the ELF load segments support 16 KiB pages.

The build fetches only the required CrispASR sources at a fixed revision and verifies each SHA-256. It separately verifies the fixed ggml archive and disables its process-wide crash handler and debugger launcher. Use `--cache` to reuse the verified downloads across builds. Keep the model and runtime artifacts outside version control and include the [third-party notices](licenses/) when distributing a build with Rumble2 Flash.

## Inference

Pass mono Float32 samples at 16 kHz, normalized to ±1, in clips from 100 ms through 10 seconds. Callers are responsible for verifying the model checksum and choosing their existing transcription fallback when the runtime returns no text. For automatic language detection, leave the language unset. Use `jaro_rumble_transcribe_detected` to receive the language name alongside the transcript. To force a language, pass its English name.

Assign a unique nonzero ID to each call. Cancellation is checked between the mel, encoder, and decoder stages and after every generated token. Keep the model handle alive until all calls with it have returned. Free the returned UTF-8 string with `jaro_rumble_free_text`.
