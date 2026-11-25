<picture>
  <source media="(prefers-color-scheme: dark)" srcset="public/jard-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="public/jard-light.png">
  <img src="public/jard-light.png" alt="Jaro logo" width="96" height="96">
</picture>

# Jaro — By Jard

Jaro is a local audio app for macOS and Linux. Dictate into the app you’re already using, record meetings, or transcribe an audio file. Transcription runs on your computer.

**[Download for Mac](https://github.com/AdamGera/Jaro/releases/latest/download/Jaro-arm64.dmg)** · **[Download for Linux](https://github.com/AdamGera/Jaro/releases/latest)**

## Meetings

Record your microphone, Mac audio, or both. You can also open a recording you already have.

![Illustrated dark-mode demo. Open a saved session from Home, read its overview, jump through timeline chapters, follow the transcript, and explore each speaker’s activity.](docs/media/meeting-demo.gif)

Open **Overview** for an editable summary or **Timeline** to jump to a topic. Search the **Transcript** and click a timestamp to hear that moment. **Speaker analytics** shows when each person spoke and their share of speaking time.

Type or dictate in **My Notes** to add context, then choose **Update overview** to use it in the summary. You can also ask a question in **Chat**.

## System Dictation

Use your voice to reply to email, share an update in Slack, prompt Claude, or start a draft in Google Docs. The demo shows a preview of System Dictation.

![Jaro dictation demo. Hold Right Option ⌥ and speak, then release to insert text at the cursor in email, Slack, Claude, and Google Docs.](docs/media/dictation-demo.gif)

1. Open **Settings → Dictation** and allow **Microphone** and **Accessibility** access.
2. With Jaro running, click where you want the text to go. Hold **Right Option** and speak.
3. Release **Right Option**. Jaro inserts the text at your cursor.

Double-tap **Right Option** for hands-free dictation, then press it again to finish. [Dictation settings and shortcuts](docs/system-dictation.md)

## Install

### macOS

Before downloading, open **Apple menu → About This Mac**. Jaro requires an **Apple chip** (such as M1) and **macOS 26 or later**. Finder may show a crossed-out app icon if your Mac doesn’t meet these requirements. [Apple’s guide to identifying your Mac’s chip](https://support.apple.com/en-us/116943)

Open the DMG, drag the app to **Applications**, and open it. First launch downloads about 4 GB of models. After setup, transcription, speaker labeling, and summaries work offline.

Allow microphone or Mac audio access when you first record from those sources. The app is signed by Jard, and macOS may require first-launch approval.

To use the same session library in your browser, choose **File → Open in Browser**.

### Linux x64

On Debian 13 or Ubuntu 24.04 LTS, download the matching `.deb` from the [latest release](https://github.com/AdamGera/Jaro/releases/latest). Install it with `sudo apt install ./filename.deb`, open Jaro, and choose **Download models**. Debian 13 also has a per-user install with **Restart to Update**. See the [Linux setup guide](desktop/portable/linux/README.md) for the installer, desktop shortcuts, and audio permissions.

## Mobile previews

Jaro’s native mobile keyboards let you dictate into other apps, swipe to type, and manage your vocabulary on your device. Both ports are available to build from source.

| Platform | Requirements | Build and setup |
| --- | --- | --- |
| iPhone and iPad | iOS or iPadOS 18+. Some dictation features require iOS or iPadOS 26 and supported hardware. | [iPhone and iPad guide](mobile/ios/README.md) |
| Android | Android 12+ on ARM64 | [Android guide](mobile/android/README.md) |

Public mobile downloads are pending device testing and release signing. [Mobile release status](mobile/README.md) · [Mobile privacy](mobile/PRIVACY.md)

## Develop and contribute

The [documentation index](docs/README.md) has the user guides, SDK, and build instructions. See [Contributing](CONTRIBUTING.md) for the source layout and checks, the [Roadmap](ROADMAP.md) for current work, or [Security](SECURITY.md) to report a vulnerability.

## Why we made it

At Jard (Joint AI Research & Dynamics), we believe intelligence should live on the edge, on the devices we use every day. A larger model is worth bringing in when a task needs more capability.

Recent advances in speech recognition and compact language models have made dictation, transcripts, and meeting summaries practical on a personal computer. We built Jaro to put that research to work today, with your recordings and notes staying on your device. We expect more to become possible on-device as the research progresses.

Jaro is free and open source under Apache 2.0, with separate licenses for the mobile source and bundled components. Download it and try it, contribute an improvement, or fork it and build your own version. [License](LICENSE) · [Component credits](NOTICE) · [Mac build details](desktop/build-tools/README.md) · [About Jard](https://www.jard.ai/jaro)
