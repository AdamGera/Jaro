# Linux desktop installation

This build targets Debian 13 amd64. Build on that target with Python 3.13 and pip, Git, desktop-file-utils, pkg-config, a C compiler, GTK4 and WebKitGTK 6.0 development files, `libgtk4-layer-shell-dev`, `libx11-dev`, `libxext-dev`, Bun, Node 24, the Native SDK CLI 0.11.1, and Zig 0.16.0.

```sh
bun install --frozen-lockfile
bun run build
python3.13 desktop/portable/build.py linux
python3.13 desktop/portable/linux/package.py \
  --host desktop/out/portable/linux/bin/jaro \
  --sdk "$NATIVE_SDK_PATH"
```

`NATIVE_SDK_PATH` points to the installed `@native-sdk/cli` package directory. The packaging step verifies the Linux CPU wheels against `requirements.txt`. Supply `--wheelhouse /path/to/wheels` for an offline build. FFmpeg, the system Python interpreter, and the desktop libraries come from Debian. The app keeps its Python dependencies separate from the user's Python installations.

Install the package with `sudo apt install ./jaro_<version>_amd64.deb`. Launch Jaro from the desktop menu or run `jaro`, then choose **Download models** on Home. If you already have the pinned assets in a local cache, use `jardctl model install --all --from /path/to/model-cache` to verify and copy them offline.

Opening Jaro again brings its running window forward, including when it is minimized. The running engine keeps ownership of the library and any active recording.

**Settings → General → Application → Launch at login** starts the local engine when you sign in. It starts off. If you already enabled a dictation shortcut and installed the speech model, dictation can be ready before you open the workspace. Open Jaro from the desktop menu to show the workspace.

On desktops with a StatusNotifier tray, close the workspace to keep Jaro in the tray. Use **Open Jaro** to return, **Record Meeting** or **Stop & Save** to control a recording, **Copy last meeting transcript** to copy the latest finished transcript, and **Quit Jaro** to exit. Closing the workspace exits on desktops without a tray host. Copy uses the desktop clipboard tools `xclip` on X11 and `wl-copy` on Wayland.

Open **Meetings → Record Meeting** to capture your microphone and computer audio across connected hardware outputs, including a headset selected by the call app. On systems with virtual outputs only, the recorder captures the default output. Use **View live transcript** during capture and **Stop & Save** when finished. The final transcript uses Rumble2 Flash and Nemotron on the mix. The session keeps the microphone and computer audio tracks and offers Markdown and subtitle exports. Interrupted meetings can be recovered from Home.

In **Settings → Meetings**, control local Qwen titles and summaries, meeting retention, audio retention, and optional microphone processing. StuPASE and microphone boost start off. Processing changes the audio used for recognition and keeps the original audio available for playback. Expired meetings go to the desktop Trash as recoverable archives.

Under **Detected calls**, turn on offers and choose Ask, Always record, or Never offer per supported app. Offers and recording controls can appear on the desktop while the workspace is closed and the login engine is running. X11 and Wayland desktops with layer-shell show a small panel. Other Wayland desktops use the desktop notification service, with buttons if it supports actions. You can also respond inside Jaro. Starting from the desktop uses automatic speech language detection. Browsers can offer after sustained microphone use, without identifying the site. When **Stop when the call ends** is enabled, automatically started meetings keep recording while the call app still uses the microphone or plays audio. After both stop, Jaro gives you 30 seconds to use **Keep recording**. The countdown also clears if the call app resumes audio.

Open **Options** from a recording offer to choose **Never offer** for that app or site. When **Always record** is available, it starts this recording on request and saves the rule for future calls. Opening or canceling the options leaves the policy alone.

Use **Settings → Dictation → Voice input** to choose a dictation microphone. Jaro follows the system default until you select one, and falls back to the system default for a take if your selected microphone is unavailable at startup.

**Noise cleanup (StuPASE)** and **Boost microphone (+6 dB)** are independent and both start off. Cleanup downloads 2.3 GB of model weights on first use and runs locally before transcription. Boost applies a peak limiter and stays active if cleanup is unavailable. These settings apply to system-wide dictation, and History keeps the original microphone audio.

In the same section, choose **Language** to give Rumble2 Flash a spoken-language hint for system-wide dictation. **Detect automatically** is the default.

Choose **Top**, **Bottom**, or **Off** under **Settings → Dictation → Feedback** to place the recording indicator. It leaves the focused app and pointer input alone. Wayland uses a layer-shell surface where available, or the existing XWayland display on desktops without layer-shell.

Use the same section to mute dictation cues, choose a sound style, preview its start sound, or set cue volume. Jaro uses the default playback device.

Under **While dictating**, choose whether to leave playback alone, mute or lower other apps on the default output, or pause compatible media players. Jaro restores playback when the take ends and preserves volume changes you make during the take.

Jaro asks the desktop to prevent idle sleep while it records dictation, audio, or video. When recording ends, Jaro releases the request and the system resumes its usual power policy.

Add names and alternate spellings with **Vocabulary** under **Voice input**. Enter whole-phrase rules such as `kitchen table => studio desk` under **Dictation → Output** or **Settings → Dictation**. Rules apply to new system dictations and can hold up to 4,000 characters. History shows the corrected text and keeps the original audio.

Use **Settings → Dictation → Shortcuts** to choose how a registered X11 or Wayland portal shortcut starts and finishes dictation. **Tap to start and stop** is the default. **Hold** finishes when you release the shortcut. **Hold or double-tap** also lets you double-tap to keep recording hands-free. **Double-tap only** starts on two quick taps. Press the shortcut once to finish a hands-free take.

When a global shortcut ends a take, Jaro keeps 200 ms of audio for the last syllable. If you're still speaking, it waits for a pause for up to 600 ms total. Workspace and command stops end capture immediately.

If the microphone stops delivering audio mid-dictation, Jaro transcribes the speech it already heard and copies it to the clipboard. You can review it in **Dictation → History** when History is enabled, or use **Copy last** if the desktop blocked clipboard access.

You can start another dictation while Jaro processes the first. Jaro finishes takes in order and waits until the microphone closes before pasting. X11 keeps each take tied to its original field. On Wayland, older overlapping takes also paste when the desktop accessibility service confirms that the original field still has focus. Otherwise, they stay available in History when enabled.

On Wayland with the AT-SPI accessibility service, back-to-back takes and **Paste Last** join when a supported editor confirms the same field and caret position. Typing, moving the caret, selecting text, or switching fields ends the join. Jaro checks editor metadata locally and does not fetch the text in the field.

Chromium must expose its editor to the desktop accessibility service for joining to work. If Jaro observes the original field and it loses focus before paste, the transcript stays available in History or through **Copy last**. [Chromium accessibility](https://new.chromium.org/developers/accessibility/testing/automated-testing/ax-inspect/)

Choose **Enable shortcut** under **Dictation → General** to use the global shortcut. Jaro reconnects after restart once the speech model is ready. On Wayland, restoration requires a saved assignment from the desktop. If the keys are unavailable, choose **Enable shortcut** to retry or **Disable on launch** to clear the saved preference. [Global Shortcuts portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.GlobalShortcuts.html)

Choose **Cancel dictation** from **Dictation → General** while the take is starting, listening, or processing. On X11, **Escape** cancels a hands-free take. Wayland requests **Ctrl+Alt+Shift+X** at shortcut setup, and shows the desktop’s assigned key when it grants one.

With Dictation History on, open **Dictation → History** and select an entry to play its original microphone audio for up to 24 hours. Dictation text follows your History retention setting. Deleting an entry or clearing History also deletes its audio.

On GNOME Wayland, open **Dictation → General** and select **Add GNOME shortcuts**. Keep Jaro open while using the keys. Press **Ctrl+Alt+Shift+D** once to begin and again to finish. Use **Ctrl+Alt+Shift+X** to discard the take, or **Ctrl+Alt+V** to paste the last dictation after allowing keyboard and clipboard access. Existing setups can choose **Add cancel shortcut**. If a default key is already assigned, choose a free key in **GNOME Settings → Keyboard → Custom Shortcuts**. Choose **Remove GNOME shortcuts** to remove Jaro’s bindings while keeping manual commands available.

On Wayland, choose **Allow paste** and approve both keyboard and clipboard access. If the desktop remembers the grant, Jaro can reconnect after restart. Choose **Disable paste** to end that session and remove Jaro’s saved grant. **Copy last** remains available without automatic paste. [Remote Desktop portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html)

In a saved session’s Overview, open **Add images** to import an image or take a screenshot through your desktop picker. The picker offers the capture modes it supports. Jaro adds the approved image to the session. Some desktops also copy it to the clipboard. [Screenshot portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Screenshot.html)

On Home, open **Record screen**, choose **Display** or **Window**, and approve the share in your desktop’s picker. Enable **Microphone narration** or **Computer audio** to record the default input or output. Computer audio includes everything playing through that output. The recorder shows a meter for each enabled source. When both are enabled, Jaro transcribes the mix locally with speaker labels and keeps the individual tracks in the MP4. Select **Stop and save**, then play the MP4 in the session’s Overview. These controls require a desktop that offers the ScreenCast portal and the `gstreamer1.0-tools` and `gstreamer1.0-pipewire` packages. [ScreenCast portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html)

To capture a portion of a display, choose **Area** and approve the display in the picker. Drag on the preview to select the area, then choose **Record area**. If Jaro covers the content you want to select, click **Refresh preview** and switch to the other window within three seconds. Jaro keeps the full-display preview in memory and records only the confirmed area.

On a Wayland desktop with layer-shell support, choose **Draw on screen** during a display or area recording. Draw directly over the shared content, then press Esc or right-click to return to normal pointer control while recording continues. Strokes fade after you lift the pointer and appear in the saved video.

For window recordings and desktops without direct drawing, choose **Draw** to mark the live preview. Choose **Clear** to remove the visible strokes or **Done drawing** to close the preview and keep recording. Jaro saves the drawings in the MP4. Keep Jaro outside the capture to leave its controls out of the video.

If Jaro closes before you save, reopen it and select **Recover** under **Interrupted screen recording** on Home. Jaro saves the playable portion as a session and transcribes any captured audio. Choose **Discard…** to remove the local draft after confirming.

Jaro checks for updates shortly after launch and hourly while it is open. Choose **Look for new versions** next to Settings or in **Settings → General** to check immediately. For the Debian package, **Download Update** opens the newer package in your browser. Close Jaro and wait for its CLI jobs before installing it through the Debian package manager. `sudo apt purge jaro` removes the packaged app. Sessions and models remain in `${XDG_DATA_HOME:-$HOME/.local/share}/jaro`.

## Direct installation with Restart to Update

On Debian 13 amd64, install the system packages listed in the Debian build's dependencies, plus `openssl`. Download `Jaro-linux-install.py`, `Jaro-linux-x64.tar.gz`, and `Jaro-linux-x64.update.json` from the same trusted Jaro release. Run the installer as your everyday user.

```sh
python3.13 Jaro-linux-install.py Jaro-linux-x64.tar.gz Jaro-linux-x64.update.json
```

Open Jaro from the desktop menu. The stable launchers are `jaro` and `jardctl` under `${XDG_DATA_HOME:-$HOME/.local/share}/jaro-app`. Jaro verifies each release against its installed publisher key before offering **Restart to Update**. Finish recordings and dictations before restarting. The installer keeps the previous version for rollback and removes older app copies. It reopens the previous version if the new launcher reports an early startup failure.

To build the direct release on Debian, use the release team's Native SDK update seed or Ed25519 PEM key. A custom publisher can supply its trusted public key with `--publisher-key` at build and initial install.

```sh
python3.13 desktop/portable/linux/direct_package.py path/to/jaro_<version>_amd64.deb \
  --signing-key "$JARO_RELEASE_KEY_FILE" --out desktop/out/portable/linux/direct
```

Publish all three outputs on the same stable GitHub release. The bundled installer expects the archive and the signed manifest from that release.

To remove a direct install, quit Jaro, then run `python3.13 Jaro-linux-install.py uninstall`. This removes the application and its desktop and login entries. The recording library and model downloads remain available for a reinstall.

This build is for local testing until the public download has a verified publisher and the desktop release checks pass.
