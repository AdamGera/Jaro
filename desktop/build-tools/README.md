# Building the Mac app

Use an Apple Silicon Mac on macOS 26 with Xcode command-line tools, Node 24+, Bun, uv, Rust 1.97 with Cargo, and Native SDK CLI 0.11.1. Install the CLI on the build machine with `npm install -g @native-sdk/cli@0.11.1`. The builder uses Zig 0.16.0 and downloads a checksum-verified copy when needed. The shell links the pinned Native SDK AppKit host and uses the system WKWebView to display the same built React application as the browser. The build uses Cargo’s locked dependencies for the isolated JavaScript transformation helper. Users installing the finished app need only macOS.

Build the local disk image from the repository root.

```sh
python3 desktop/build-tools/package.py --dmg
```

The builder writes the app and drag-to-Applications DMG to `desktop/out/`. The build uses the configured Jard publisher identity when present. A fresh development checkout without that identity produces an ad-hoc local build. For a notarized build, use your Developer ID Application identity and a Keychain profile previously configured with Apple's `notarytool`.

```sh
python3 desktop/build-tools/package.py --dmg \
  --identity "Developer ID Application: YOUR TEAM NAME (TEAM_ID)" \
  --notary-profile "YOUR_KEYCHAIN_PROFILE"
```

The build verifies CPython and llama.cpp archives, installs hash-locked wheels into a portable Python runtime, and compiles the required FFmpeg decoders from upstream source. PyTorch, FFmpeg, FFprobe, the language catalogs, and the Metal-enabled writer executable are included inside the app. The model weights are installed separately at first use. Setup can download the pinned files or copy them from an existing `model/` directory. Progress and checksums apply to either source.

For an isolated test, set `RUMBLE_DATA_DIR` to a fresh private temporary directory, `RUMBLE_LOG_DIR` inside that directory, and `RUMBLE_PORT` to an unused local port. These variables work with the bundle's executable and helpers. This keeps fixtures separate from the everyday session library. A packaging smoke test can use `Contents/Helpers/jard-engine model-status`, followed by `Contents/Helpers/jard-engine models --from /path/to/model` and `Contents/Helpers/jard-engine start`.

## Local iteration

Use `/Applications/Jaro.app` for local UI checks. Build and sign a temporary staging bundle with the same publisher. Quit using Jaro's regular capture and engine guards, wait for the engine to release its loopback listener, then replace the app at the installed path and reopen it against the same library. Verify that the workspace page and its assets load and use macOS Trash for the superseded bundle and other generated copies. Keep the staging bundle closed until it is installed.

## Updating the sound preview

Regenerate the Settings preview after changing the indicator renderer or `desktop/assets/dictation-sounds/preview.json`. The same schedule controls native cue playback and the web preview.

```sh
preview_build=$(mktemp -d)
xcrun swiftc -O -target arm64-apple-macos26.0 \
  desktop/indicator/DotScopeSimulation.swift desktop/indicator/JardDictationMeter.swift \
  desktop/build-tools/render-sound-preview.swift -o "$preview_build/render"
"$preview_build/render" desktop/assets/dictation-sounds/preview.json src/assets/dictation-preview.png
```

## Desktop preferences from the CLI

`jardctl settings list` includes `ui_language` and `show_dock_icon`. Use `settings get ui_language` to read the saved choice, `settings set ui_language fr` to select French, or `settings set ui_language system` to follow macOS. `settings set show_dock_icon off` makes the menu bar the way back to a closed workspace. The Dock icon stays visible when the workspace needs it.

`settings apply` accepts both application and speech preferences in one JSON document and can save them while Jaro is closed. `ui_language` configures the interface. Set `dictation.languages` separately when the speech hints need to change.

Use `jardctl history --search 'phrase' --json` to search retained dictations even with Jaro closed. History keeps up to 10,000 takes or 16 MiB of combined raw and final text, whichever limit it reaches first. A newly saved raw or final field holds up to 16 KiB of UTF-8 text. The current dictation is delivered in full. The workspace and CLI read the same retained History.

## Reading and watching meetings

The bundle includes `jardctl` in `Contents/Helpers`. The saved-meeting commands read the local session database directly and also work with Jard closed.

```sh
jard='/Applications/Jaro.app/Contents/Helpers/jardctl'
"$jard" meetings list --since 7d --json
"$jard" meetings search '"launch review" pricing' --until today --json
"$jard" meetings show latest --from 01:30 --to 02:15 --md
"$jard" meetings show latest --vtt
"$jard" meetings watch
```

Use the same executable with the `mcp` argument for a local stdio MCP client. It exposes meeting discovery, transcript and notes search, timed transcript reads, saved notes, and `jard-meeting://{meeting_id}` Markdown resources. The CLI and MCP label the rolling transcript while Jard records and finalizes it. Interrupted meetings retain readable provisional text when it was saved. They use the same meeting ID after recovery. Use `--no-in-progress` or `include_in_progress: false` to exclude current recordings and finalizations. Date filters match the meeting's full time span, and a date in `--until` includes that entire local day.

Markdown meeting reads include the saved Quick Brief, Overview, and My Notes when available. Use `--from` and `--to` to excerpt the transcript without cutting speaker turns. JSON returns the requested bounds in milliseconds, and Markdown identifies the range above the transcript. The MCP transcript tool accepts `start` and `end` or `from` and `to` using the same offsets. Subtitle exports retain each selected turn's position from the beginning of the recording.

When Jard reopens, it resumes pending meetings from their last committed audio checkpoint once the model is ready and the recorder is idle. Interrupted sessions show `finalizationPending` while waiting. Two unexpected stops during recovery leave the recording and available transcript in Sessions under the original ID.

Meetings Settings controls whole-meeting retention. The 24-hour default covers new meetings, while an explicit setting applies to existing completed meetings too. Expired meetings disappear from Sessions and companion reads. Their audio, transcript, and notes are available together in the macOS Trash. A separate audio window or the confirmed Move meeting audio to Trash action moves only the recording and its source tracks. The transcript and notes stay in Sessions for the lifetime of the meeting.

`meetings watch` emits `{"type":"meeting.finished","id":"…"}` after the final transcript commits. Integrations can use `GET /api/meetings/events` on the local engine with `Authorization: Bearer <native token>`. The token is stored in `.native-token` under the library root. The stream permits one authenticated subscriber, sends keep-alive comments, and carries only the meeting ID. On reconnect, subscribe first, list meetings since the last observed date, and deduplicate by ID.

Model provenance and SHA-256 digests live in `models.json`. The release notes identify ad-hoc builds. For a Developer ID release, verify notarization and stapling and test the downloaded DMG on a separate clean Apple Silicon Mac.

To run the packaged smoke test, pass the built app path to `desktop/build-tools/smoke-package.py`. Add `--models /path/to/model` to exercise real local inference, word timing, speaker labels, and playback. The test creates a temporary copy of the verified weights, runs the bundle using the system PATH, and removes its own library on exit.

## Signing successive builds with the same publisher certificate

For builds distributed without an Apple Developer ID, the packager accepts a publisher-owned code-signing certificate. Use the same certificate and private key for each version, including local installs intended to replace the public app. The certificate identifies the app and its recorder across builds. The separate Ed25519 update key authenticates the GitHub feed.

Install the upstream [rcodesign 0.29.0](https://github.com/indygreg/apple-platform-rs/releases/tag/apple-codesign%2F0.29.0) executable on the build machine. The builder also looks in `~/.local/bin/` for the pinned tool. Store the PEM-encoded code-signing certificate as `certificate.pem` and its private key as `private-key.pem` in a private directory outside the checkout. Restrict the private key to its owner and keep a secure backup of both files. Losing or regenerating the certificate changes the app’s identity. Users receive the signed app in the usual DMG or update ZIP.

```sh
python3 desktop/build-tools/package.py --dmg \
  --publisher-identity "$HOME/.config/jard/soundfolio-local/macos-publisher" \
  --update-key "$HOME/.config/jard/soundfolio-local/native-update.key" \
  --notes "Describe what changed in this version."
```

The maintainer’s default identity directory is `~/.config/jard/soundfolio-local/macos-publisher/`. An update built with this signing mode must match `desktop/build-tools/jard-publisher.crt`. Restore the original identity from backup when moving the build to another Mac. Forks using their own updater should pin their own public certificate.

The packager verifies the app’s signature and checks the signer of both the foreground app and the capture helper. Moving from an ad-hoc release can require a fresh grant. Gatekeeper’s first-launch policy still applies. [Apple’s explanation of app identity](https://developer.apple.com/documentation/technotes/tn3127-inside-code-signing-requirements)

## Publishing in-app updates

Increment the version in `desktop/app.json` and run the build from a clean checkout. Jard's update public key is in that manifest. The private Ed25519 seed belongs to the release maintainer. Keep a secure backup outside the repository. Installed apps trust that key for future updates.

```sh
python3 desktop/build-tools/package.py --dmg \
  --update-key "$HOME/.config/jard/soundfolio-local/native-update.key" \
  --notes "Describe what changed in this version."
```

Create a draft GitHub release tagged `v<version>` at the build commit. Attach the DMG as `Jaro-arm64.dmg`, the matching source as `Jaro-native-source.zip`, and both generated update files, `Jaro-arm64.zip` and `native-update.json`. Publish it as the latest stable release once all four assets are present. The app reads the feed from the latest release and stages the version-specific ZIP in the background after validating the signed feed. The user chooses Restart to Update when the verified bundle is ready. Leave the assets at their published URLs.

To offer a Development channel, generate a separate Ed25519 feed key and provide `--dev-updates /path/to/development.json` when packaging both channels. This file supplies the Development HTTPS `feed_url`, the matching Base64 `public_key`, and an optional HTTPS `release_url` for manual downloads. `--channel dev` stamps a Development build. Pass the Development seed to `--update-key` for that build and publish its archive under `dev-v<version>` as a prerelease. Publish the signed `native-update.json` at the configured Development feed URL. Continue signing each application bundle with the same publisher certificate. Omit `--dev-updates` for bundles that follow the Stable feed alone.

For a Development release, give `--notes` one change per line. Attach the generated `desktop/out/Jaro-development-notes.json` to the prerelease and pass that file as `--previous-dev-notes` when packaging the next Development release. The first release can omit it. The packager embeds the resulting rolling index in the signed app and also includes the current release's plain notes in the signed feed for older Jaro versions. A user who skipped Development releases sees the distinct changes since the version they have installed.

### Gradual releases

For a gradual release, add `--rollout-percent 10` to the signing command. The packager writes both feed formats. New builds check `native-update-v2.json` first and compare their private local bucket against the signed percentage. On HTTP 404 they check the regular feed. `--rollout-required` makes the candidate available to every eligible build when published to the rollout endpoint. Installation still uses Restart to Update or Quit.

For Stable, publish the versioned archive at its signed URL on a prerelease. Attach `native-update-v2.json` to the separate `stable-rollout` prerelease. The previous Stable release stays Latest so existing installs can continue checking the regular feed. After the rollout, attach `native-update.json` to the candidate release and promote it to Latest, then remove the rollout feed asset so subsequent releases can use the normal feed. Keep the versioned archive available. Development can use an optional separate HTTPS `rollout_url` in `--dev-updates` and the same promotion sequence at its configured endpoints. `--stable-rollout-url` overrides the default for a self-hosted feed.

Re-sign the same release to increase its percentage without rebuilding the app or archive. Run these commands from the repository root.

```sh
xcrun swiftc -O desktop/update/UpdateRollout.swift desktop/build-tools/sign-update-rollout.swift -o /tmp/jaro-sign-update-rollout
/tmp/jaro-sign-update-rollout desktop/out/native-update.json /tmp/native-update-v2.json \
  "$HOME/.config/jard/soundfolio-local/native-update.key" stable 50 false
```

Replace the rollout feed asset with the resulting file. The signing command verifies that the regular feed was signed with the supplied channel key. For Development, use `dev` and its own key.

Forks distributing their own builds should generate their own key with `native update keygen --private-key /private/path/native-update.key` and configure their public key and release URLs before shipping.
