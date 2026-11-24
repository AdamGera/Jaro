# Documentation

## Use Jaro

- [Install and use the Mac app](../README.md)
- [System Dictation setup, shortcuts, and settings](system-dictation.md)
- [TypeScript SDK and local transcription service](../sdk/typescript/README.md)

## Develop and release

- [Repository layout and development checks](../CONTRIBUTING.md)
- [Build and test the signed Mac app](../desktop/build-tools/README.md)
- [Security reporting](../SECURITY.md)
- [Roadmap](../ROADMAP.md)

## Desktop workspace parity

The macOS, Windows, and Linux builds share the session workspace. Keep transcript
search and copy, overview actions, and timeline regeneration in the top toolbar.
Verify the transcript search shortcut (Command+F on macOS, Ctrl+F on Windows and
Linux) and the hover tooltips on each desktop build.

Run the playback regression in `desktop/tests/web_parity.py` with `--playback`.
It verifies that a session already open during processing can play audio and show
the waveform as soon as it completes. Use the optional Chromium driver for
additional coverage, then check playback in the packaged Windows and Linux apps.
