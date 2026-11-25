# Roadmap

Jaro is a macOS and Linux desktop app with local transcription, system-wide dictation, searchable dictation history, meeting recordings and notes, and a local TypeScript SDK. The desktop builds also include annotated screen capture and a signed updater. See the [user and developer guides](docs/README.md) for current entry points.

## Verify core macOS workflows

- Exercise physical dictation shortcuts, permission recovery, and insertion into more apps.
- Test meeting recovery and audio route changes during real calls. Verify calendar consent and reminders on a test calendar.
- Check screen selection, annotation, and delivery with real macOS permissions and displays.
- Verify the public signed upgrade path from an older install and confirm the installed permission grants survive it.

## Explore experimental workflows

After the core verification, evaluate expanded shortcut bindings, live transcription
controls, and additional meeting and capture workflows with Jaro's local models.

## Validate mobile previews

- Test dictation in other apps, keyboard permissions, and offline behavior on physical iPhone, iPad, and Android devices.
- Verify iOS signing and the App Group. Verify Android release signing and upgrading in place.
- Track Rumble2 Flash integration per mobile platform through device memory, latency, battery, and offline checks. See the [mobile build and release guides](mobile/README.md) for the current speech engines.

Published desktop builds cover Apple Silicon macOS, Debian 13 x64, and Ubuntu 24.04 LTS x64. The Windows 11 x64 build is in validation.
