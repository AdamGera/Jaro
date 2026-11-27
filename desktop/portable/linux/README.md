# Linux desktop installation

Download the matching x64 package for Debian 13 or Ubuntu 24.04 LTS from [GitHub Releases](https://github.com/AdamGera/Jaro/releases).

## Build from source

Build on amd64 Ubuntu 24.04 with Python 3.12 or Debian 13 with Python 3.13. Use the target distribution’s Python and pip, Git, desktop-file-utils, pkg-config, a C compiler, GTK4 and WebKitGTK 6.0 development files, `libx11-dev`, `libxext-dev`, `libwayland-dev`, `libwayland-bin`, `libxkbcommon-dev`, Bun, Node 24, the Native SDK CLI 0.11.1, and Zig 0.16.0.

On Debian, install `libgtk4-layer-shell-dev`. On Ubuntu, install `curl`, `meson`, `ninja-build`, and `wayland-protocols`. The Ubuntu build verifies the pinned GTK4 layer-shell 1.0.4 source and bundles the compiled library for its overlay helpers. [GTK4 layer-shell source](https://github.com/wmww/gtk4-layer-shell/tree/v1.0.4)

```sh
bun install --frozen-lockfile
bun run build
python3 desktop/portable/build.py linux
python3 desktop/portable/linux/package.py \
  --host desktop/out/portable/linux/bin/jaro \
  --sdk "$NATIVE_SDK_PATH"
```

`NATIVE_SDK_PATH` points to the installed `@native-sdk/cli` package directory. The packaging step verifies the Linux CPU wheels against `requirements.txt`. Supply `--wheelhouse /path/to/wheels` for offline Python dependency installation. FFmpeg, the system Python interpreter, and the main desktop libraries come from the target distribution. The app keeps its Python dependencies separate from the user's Python installations.

Install the package for your distribution with `sudo apt install ./jaro_<version>-<revision>_ubuntu24.04_amd64.deb` on Ubuntu or `sudo apt install ./jaro_<version>-<revision>_amd64.deb` on Debian. Launch Jaro from the desktop menu or run `jaro` to open guided setup. It prepares local models, helps enable the shortcut and Wayland paste access, and lets you try dictation. If you already have the pinned assets in a local cache, use `jardctl model install --all --from /path/to/model-cache` to verify and copy them offline.

Choose **Settings → Dictation → Replay setup** or run `jardctl onboarding reset` while Jaro is running. The tour checks existing models, approvals, and shortcuts. You can skip practice or finish setup later.

On Wayland, choose **Configure shortcuts** if your desktop leaves the dictation key unassigned. You can cancel a pending desktop approval and retry it. On KDE Plasma, replay setup once after upgrading from a build that used the older shortcut registration. If automatic paste is unavailable or you prefer to leave it off, choose **Continue without automatic paste**. Dictate with your shortcut, then use **Copy last** in Dictation and paste manually.

If you installed Rumble2 Flash alone, use **Settings → General → Local models** in the desktop app to add speaker labels, word timing, and writing support. Speech remains available during setup.

If the installed models fail to start, choose **Repair models**. Jaro checks the pinned files and reuses verified copies. You can also quit Jaro and run `jardctl model install --all --repair`. Add `--from /path/to/model-cache` to repair from an offline cache.

Opening Jaro again brings its running window forward, including when it is minimized. The running engine keeps ownership of the library and any active recording.

**Settings → General → Application → Launch at login** starts the local engine when you sign in. It starts off. If you already enabled a dictation shortcut and installed the speech model, dictation can be ready before you open the workspace. Open Jaro from the desktop menu to show the workspace.

On desktops with a StatusNotifier tray, close the workspace to keep Jaro in the tray. Use **Open Jaro** to return, **Record Meeting** or **Stop & Save** to control a recording, **Copy last meeting transcript** to copy the latest finished transcript, and **Quit Jaro** to exit when the engine is idle. Without a tray, closing the workspace leaves any active recording, transcription, or model download running. Reopen Jaro to return to the same workspace. The engine exits when it finishes its work and the workspace is closed. Copy uses the desktop clipboard tools `xclip` on X11 and `wl-copy` on Wayland.

Choose **Microphone + computer audio** for an ordinary session that includes the default playback output. The microphone starts even if the computer is quiet. The saved session includes both original tracks and their mix.

Open **Meetings → Record Meeting** to capture your microphone and computer audio across connected hardware outputs, including a headset selected by the call app. On systems with virtual outputs only, the recorder captures the default output. Use **View live transcript** during capture and **Stop & Save** when finished. The final transcript uses Rumble2 Flash and Nemotron on the mix. The session keeps the microphone and computer audio tracks and offers Markdown and subtitle exports. Interrupted meetings can be recovered from Home.

Use **New session** for a regular recording. Sessions and Meetings share the same workspace, and saved recordings resume transcription after a restart from their last committed checkpoint. See the [shared recording workflow](../../../docs/recording-workspace.md).

In **Settings → Meetings**, control local Qwen titles and summaries, meeting retention, audio retention, and optional microphone processing. StuPASE and microphone boost start off. Processing changes the audio used for recognition and keeps the original audio available for playback. Expired meetings go to the desktop Trash as recoverable archives.

With Jaro running, use `jardctl meetings record --title "Design review"` to record
your microphone and computer audio. The title is optional. Use `jardctl meetings status`
to check the recording, `jardctl meetings stop` to save it, and
`jardctl meetings copy-last` to copy the active transcript once speech is available.
Before then, Copy Last uses the latest finished transcript. With the engine running,
`jardctl meetings show <uuid>` and `jardctl mcp` can read available live speech while
the workspace is closed.

If Jaro exits during a meeting, the CLI and MCP can read the last saved timed
transcript while the app is closed. `jardctl meetings list --no-in-progress` includes
interrupted meetings and filters out active recordings. On restart, Jaro resumes
meeting recovery under the same ID once the speech model and recorder are ready.
If the draft cannot be imported automatically, use **Recover** on Home.

Use `jardctl meetings list --json` to get the full meeting UUID,
then `jardctl meetings delete <uuid>` to move a settled meeting to the desktop
Trash. Add `--json` to check whether Trash delivery is pending.

Under **Detected calls**, turn on offers and choose Ask, Always record, or Never offer per supported app. Offers and recording controls can appear on the desktop while the workspace is closed and the login engine is running. X11 and Wayland desktops with layer-shell show a small panel. Other Wayland desktops use the desktop notification service, with buttons if it supports actions. You can also respond inside Jaro. Starting from the desktop uses automatic speech language detection. With browser accessibility available, the Calls dialog applies rules to recognized meeting sites. Ambiguous or unavailable page information can produce an offer that you confirm. When **Stop when the call ends** is enabled, automatically started meetings keep recording while the call app still uses the microphone or plays audio. After both stop, Jaro gives you 30 seconds to use **Keep recording**. The countdown also clears if the call app resumes audio.

Browser call detection uses the desktop accessibility bus to identify the visible page of the browser using the microphone. If Chromium leaves the document address blank, Jaro checks its unfocused address bar. Starting a recording automatically requires a verified address. If Chromium shows a different Google Meet room without the URL scheme, that room can still trigger the stop countdown for an already verified recording. Jaro compares temporary room identifiers without saving the page address. [AT-SPI Collection](https://raw.githubusercontent.com/GNOME/at-spi2-core/main/xml/Collection.xml)

Open **Options** from a recording offer to choose **Never offer** for that app or site. When **Always record** is available, it starts this recording on request and saves the rule for future calls. Opening or canceling the options leaves the policy alone.

Use **Settings → Dictation → Voice input** to choose a dictation microphone. Jaro follows the system default until you select one, and falls back to the system default for a take if your selected microphone is unavailable at startup.

**Noise cleanup (StuPASE)** and **Boost microphone (+6 dB)** are independent and both start off. Cleanup downloads 2.3 GB of model weights on first use and runs locally before transcription. Boost applies a peak limiter and stays active if cleanup is unavailable. These settings apply to system-wide dictation, and History keeps the original microphone audio.

In the same section, choose **Language** to give Rumble2 Flash a spoken-language hint for system-wide dictation. **Detect automatically** is the default.

Choose **Top**, **Bottom**, or **Off** under **Settings → Dictation → Feedback** to place the recording indicator. It leaves the focused app and pointer input alone. Wayland uses a layer-shell surface where available, or the existing XWayland display on desktops without layer-shell.

Use the same section to mute dictation cues, choose a sound style, preview its start sound, or set cue volume. Jaro uses the default playback device.

Under **While dictating**, choose whether to leave playback alone, mute or lower other apps on the default output, or pause compatible media players. Jaro restores playback when the take ends and preserves volume changes you make during the take.

The dictation shortcut and `jardctl dictation start` work during Meetings, Sessions, and Annotate. Jaro suspends the playback effect while recording computer audio, including when you start the recording partway through a dictation.

Jaro asks the desktop to prevent idle sleep while it records dictation, audio, or video. When recording ends, Jaro releases the request and the system resumes its usual power policy.

Add names and alternate spellings with **Vocabulary** under **Voice input**. Enter whole-phrase rules such as `kitchen table => studio desk` under **Settings → Dictation → Output and vocabulary corrections**. Rules apply to new system dictations and can hold up to 4,000 characters. History shows the corrected text and keeps the original audio.

Use **Settings → Dictation → Shortcuts** to choose how a registered X11 or Wayland portal shortcut starts and finishes dictation. **Tap to start and stop** is the default. **Hold** finishes when you release the shortcut. **Hold or double-tap** also lets you double-tap to keep recording hands-free. **Double-tap only** starts on two quick taps. Press the shortcut once to finish a hands-free take.

When a global shortcut ends a take, Jaro keeps 200 ms of audio for the last syllable. If you're still speaking, it waits for a pause for up to 600 ms total. Workspace and command stops end capture immediately.

If the microphone stops delivering audio mid-dictation, Jaro transcribes the speech it already heard and copies it to the clipboard. You can review it in **Dictation → Saved dictations** when History is enabled, or use **Copy last** if the desktop blocked clipboard access.

You can start another dictation while Jaro processes the first. Jaro finishes takes in order and waits until the microphone closes before pasting. X11 keeps each take tied to its original field. On Wayland, older overlapping takes also paste when the desktop accessibility service confirms that the original field still has focus. Otherwise, they stay available in History when enabled.

On Wayland with the AT-SPI accessibility service, back-to-back takes and **Paste Last** join when a supported editor confirms the same field and caret position. Typing, moving the caret, selecting text, or switching fields ends the join. Jaro checks editor metadata locally and does not fetch the text in the field.

Chromium must expose its editor to the desktop accessibility service for joining to work. If Jaro observes the original field and it loses focus before paste, the transcript stays available in History or through **Copy last**. [Chromium accessibility](https://new.chromium.org/developers/accessibility/testing/automated-testing/ax-inspect/)

Choose **Enable shortcut** under **Settings → Dictation** to use the global shortcut. Jaro reconnects after restart once the speech model is ready. On Wayland, restoration requires a saved assignment from the desktop. If the keys are unavailable, choose **Enable shortcut** to retry or **Disable on launch** to clear the saved preference. [Global Shortcuts portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.GlobalShortcuts.html)

Choose **Cancel dictation** from **Settings → Dictation** while the take is starting, listening, or processing. On X11, **Escape** cancels a hands-free take. Wayland requests **Ctrl+Alt+Shift+X** at shortcut setup, and shows the desktop’s assigned key when it grants one.

With Dictation History on, open **Dictation → Saved dictations** and select an entry to play its original microphone audio for up to 24 hours. Dictation text follows your History retention setting. Deleting an entry or clearing History also deletes its audio.

Use `jardctl history --text` to read retained dictations or `jardctl history clear --yes` to clear them and reset Paste Last. Clear also resets Paste Last when History is off.

With the X11 or Wayland shortcut enabled, use `jardctl dictation start`, `stop`, `toggle`, `cancel`, or `paste-last`. The same commands are available when **Enable commands** is selected on Wayland. Add `--raw` to `start` or `toggle` to skip output processing. Use `jardctl dictation stop --print` to get the active take on stdout, or add `--json` for structured output. Printed takes leave the focused field, clipboard, and Paste Last unchanged and appear as **Printed** in History when enabled. A silent take exits with code 4.

On GNOME Wayland, open **Settings → Dictation** and select **Add GNOME shortcuts**. Keep Jaro open while using the keys. Press **Ctrl+Alt+Shift+D** once to begin and again to finish. Use **Ctrl+Alt+Shift+X** to discard the take, or **Ctrl+Alt+V** to paste the last dictation after allowing keyboard and clipboard access. Existing setups can choose **Add cancel shortcut**. If a default key is already assigned, choose a free key in **GNOME Settings → Keyboard → Custom Shortcuts**. Choose **Remove GNOME shortcuts** to remove Jaro’s bindings while keeping manual commands available.

On Wayland, choose **Allow paste** and follow any permission prompt from your desktop. On Ubuntu 24.04 with GNOME, turn on **Allow Remote Interaction** and **Allow Clipboard Access**, then choose **Share**. On Sway, Jaro remembers this choice and checks for clipboard access and `wtype` support again after restart. GNOME and Plasma keep the Remote Desktop session open until you disable paste or quit. The desktop’s access indicator may stay visible while the session is open. When the compositor offers data control, Jaro preserves the clipboard formats and leaves any newer copy in place. If the desktop remembers a portal grant, Jaro can reconnect after restart. Choose **Disable paste** to end the session and clear Jaro’s saved choice. If the current desktop cannot restore virtual keyboard access, choose **Forget saved access** to clear that choice. **Copy last** remains available without automatic paste. [Remote Desktop portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html)

In a saved session’s Overview, open **Add images** to import an image or take a screenshot through your desktop picker. The picker offers the capture modes it supports. Jaro adds the approved image to the session. Some desktops also copy it to the clipboard. [Screenshot portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Screenshot.html)

On Home, open **Record screen**. On native X11, **Display** and **Area** use the monitor under the pointer and work without a ScreenCast portal. On Wayland, approve the display in your desktop’s picker. **Window** uses the picker on both desktops. The Debian package installs the required GStreamer plugins as dependencies. [X11 capture](https://gstreamer.freedesktop.org/documentation/ximagesrc/) · [ScreenCast portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html)

With Jaro running, `jardctl annotate start` records a display with microphone narration. Use `--source window` to open the desktop picker or `--source area` to select part of a display in Jaro. If you dismiss the desktop picker, run `jardctl annotate start` again to reopen it. Run `jardctl annotate status` to see the current capture, `jardctl annotate stop` to save it, or `jardctl annotate cancel` to discard it. Use `jardctl annotate context latest` to read the saved narration and frame times. Bind `jardctl annotate toggle` to one key to start the default display capture and press it again to save. A second press while the picker is open leaves the picker open.

On native X11, `jardctl annotate start --area 740,410,320,240` records that rectangle in desktop pixels. The origin is the desktop’s top left. Choose an area within one monitor with even dimensions up to 2560 × 1440. On Wayland, use `--source area`, approve sharing in the desktop picker, then select the crop in Jaro.

Bind `jardctl annotate draw` to open drawing on the current screen recording. With no active recording, it starts a display capture and opens drawing once sharing begins. A second press closes drawing while recording continues. Desktops without the native pen open Jaro’s live recording preview, where **Done drawing** also closes it.

With Jaro and OpenCode v2 running locally, use `jardctl annotate destinations` to find a chat. Use its session ID and service identity with `jardctl annotate send latest --to SESSION --service SERVICE` to queue the saved narration and contact sheet in that chat. Pass a capture ID in place of `latest` to select an older recording. Read the delivery receipt with `jardctl annotate deliveries latest`. Use `jardctl annotate undo RECEIPT` to withdraw a queued copy or `jardctl annotate correct RECEIPT` to ask the chat to disregard a received copy.

In the Jaro desktop window, open **Send to your agent** beside a saved session’s **Images** gallery. Choose Claude Code, Codex, OpenCode, or T3 Code. Select an existing chat and choose **Send context** to share the saved overview, transcript, My Notes, and gallery images.

Install and sign in to the agent you use locally. OpenCode uses its running service. Claude Code, Codex, and T3 Code use their command-line tools. T3 Code receives paths to image files on this computer. For screen annotations sent to OpenCode, **OpenCode deliveries** below the gallery shows the receipt and any available Undo or correction action.

When you stop an annotation from the desktop shortcut or CLI, Jaro can identify the active app or a verified browser site. In the OpenCode picker, select a chat and choose **Remember destination** to suggest it for future matching annotations. Confirm suggested sends with **Send context**. Choose **Forget** to remove the suggestion.

Open **Settings → Annotate → Manage rules → Add rule** to set up a suggestion before recording. Enter a website domain, a project path such as `github.com/org/repo`, or the app ID reported by your desktop. Then choose an OpenCode chat. Paths match at folder boundaries, and the most specific path wins. Shared services such as GitHub require a project path. Use **Change chat** to update an existing rule. You can remove a suggestion even when OpenCode is closed.

Select **Send automatically** on a saved rule to send future matching annotations. Jaro shows the chosen chat and a three-second countdown after saving the narration. Hover to pause or choose **Cancel send** to keep the annotation in Sessions. If your desktop asks you to activate the countdown window, switch to it to start the timer. Run `jardctl annotate route ID` to check its state or add `--cancel` to cancel a pending send.

After OpenCode queues the annotation, the desktop panel offers **Undo send** for five seconds. Hover to keep the button available. The delivery receipt in Sessions and `jardctl annotate undo RECEIPT` can withdraw the annotation while it is still queued, for up to ten minutes.

Choose **Terminal folder** to match a terminal’s working folder and its subfolders. Jaro can read the folder of a supported focused terminal on X11 and on Wayland desktops that expose a verifiable active window, such as Sway and labwc. When several panes are open or the desktop shares too little focus information, run `jardctl annotate stop --from-project` from the folder you want to match. Jaro saves the folder with the annotation and uses its saved rule.

Choose **Project’s newest chat** to follow the most recently updated open chat in an absolute project folder, including related Git worktrees.

On X11, enable the shortcut in **Settings → Annotate**. Press **Ctrl+Alt+Shift+S** to record the display under the pointer with microphone narration. Press it again or click **Stop & Save** on the recording control. The shortcut can be changed independently of dictation. The control may appear in the video. Use **Draw** in the workspace to mark the live preview.

On desktops with Wayland shortcut approval, enable the shortcut in **Settings → Dictation** and assign **Annotate** in the desktop prompt. Tap it to choose a display and record with microphone narration. Tap again to save to Sessions. On displays that support desktop drawing, hold the shortcut to draw and release it to stop drawing. If you approved desktop shortcuts before Annotate was available, disable and re-enable them to request the new assignment.

On GNOME, **Add GNOME shortcuts** in **Settings → Dictation** also installs **Jaro · Annotate**. If you already use Jaro’s custom shortcuts, choose **Update GNOME shortcuts**. Annotate uses Ctrl+Alt+Shift+S when available. If another shortcut uses those keys, assign a new key in **GNOME Settings → Keyboard → Custom Shortcuts**. GNOME custom shortcuts act on keypress, so use **Draw** in the recording controls when you want to draw.

Enable **Microphone narration** or **Computer audio** to include those sources. For narration, keep **System default** or choose a microphone for the recording. If the selected microphone disconnects before recording starts, choose another input. The recorder shows a meter for each enabled source. With both enabled, Jaro transcribes the mix locally with speaker labels and keeps the individual tracks in the MP4. Select **Stop and save**, then play the MP4 in the session’s Overview.

If an audio source disconnects during screen recording, Jaro keeps recording and tries to reconnect that source. Its track stays silent until it returns. Jaro’s Debian package requires the system Python GStreamer bindings for audio capture. [GStreamer audio pipelines](https://gstreamer.freedesktop.org/documentation/inter/interaudiosrc.html)

To capture a portion of a display, choose **Area**. Drag on the preview to select the area, then choose **Record area**. If Jaro covers the content you want to select, click **Refresh preview** and switch to the other window within three seconds. Jaro keeps the full-display preview in memory and records only the confirmed area.

On a Wayland desktop with layer-shell support, choose **Draw on screen** during a display or area recording. Draw directly over the shared content, then press Esc or right-click to return to normal pointer control while recording continues. Strokes fade after you lift the pointer and appear in the saved video.

For window recordings and desktops without direct drawing, choose **Draw** to mark the live preview. Choose **Clear** to remove the visible strokes or **Done drawing** to close the preview and keep recording. Jaro saves the drawings in the MP4. Keep Jaro outside the capture to leave its controls out of the video.

If Jaro closes before you save, reopen it and select **Recover** under **Interrupted screen recording** on Home. Jaro saves the playable portion as a session and transcribes any captured audio. Choose **Discard…** to remove the local draft after confirming.

Open **Images** in the saved recording’s **Overview** to review sampled frames. Select a timestamp to jump in the video or choose **Save contact sheet** to download the PNG.

In Jaro’s desktop window, choose **Save video…** for a full MP4 copy, or choose **Trim video**, set the range, and preview it. **Save clip…** opens the system save dialog. The trimmed copy includes the drawings and all saved audio tracks.

Choose **Export annotation…** to save a `.jaroannotation` folder with the timestamped narration, strokes, and available media and images. The export keeps the original capture in Sessions.

In **Settings → Annotate → Storage**, choose a folder for future captures, reveal it in your file manager, or return to the Session library with **Use Default**. Existing recordings and interrupted takes keep their original destination. If that drive is disconnected, reconnect it to finish recovery or access those recordings. With Jaro running, the same controls are available through `jardctl annotate storage`.

Jaro checks for updates shortly after launch and hourly while it is open. Choose **Look for new versions** next to Settings or in **Settings → General** to check immediately. The desktop tray menu also offers **Check for Updates**, **Download Update**, or **Restart to Update** when available. For a `.deb` installation, **Download Update** opens the matching distribution package in your browser. Close Jaro and wait for its CLI jobs before installing the new package with APT. `sudo apt purge jaro` removes the packaged app. Sessions and models remain in `${XDG_DATA_HOME:-$HOME/.local/share}/jaro`.

## Direct installation with Restart to Update

On Debian 13 amd64, install the system packages listed in the Debian build's dependencies, plus `openssl`. Download `Jaro-linux-install.py`, `Jaro-linux-x64.tar.gz`, and `Jaro-linux-x64.update.json` from the same trusted Jaro release. Run the installer as your everyday user.

```sh
python3.13 Jaro-linux-install.py Jaro-linux-x64.tar.gz Jaro-linux-x64.update.json
```

Open Jaro from the desktop menu. The stable launchers are `jaro` and `jardctl` under `${XDG_DATA_HOME:-$HOME/.local/share}/jaro-app`. Jaro verifies each release against its installed publisher key before offering **Restart to Update**. It rechecks the manifest and archive before closing the workspace. If the staged files changed, choose **Look for new versions** to download them again. Finish recordings and dictations before restarting. The installer keeps the previous version for rollback and removes older app copies.

If an in-app download is interrupted, choose **Look for new versions** to resume it. Jaro keeps the partial download when you quit.

On builds that support startup verification, **Restart to Update** waits for the desktop workspace to connect. If the new launcher exits early or the window times out, Jaro reopens the previous version and skips the failed archive on later checks. A corrected archive can be offered under the same version number.

To build the direct release on Debian, use the release team's Native SDK update seed or Ed25519 PEM key. A custom publisher can supply its trusted public key with `--publisher-key` at build and initial install.

```sh
python3.13 desktop/portable/linux/direct_package.py path/to/jaro_<version>_amd64.deb \
  --signing-key "$JARO_RELEASE_KEY_FILE" --out desktop/out/portable/linux/direct
```

Publish all three outputs on the same stable GitHub release. The bundled installer expects the archive and the signed manifest from that release.
Jaro gets the archive checksum from the signed manifest, including when the GitHub release API omits its checksum. It checks the signed manifest again before restarting.

To remove a direct install, quit Jaro, then run `python3.13 Jaro-linux-install.py uninstall`. This removes the application and its desktop and login entries. The recording library and model downloads remain available for a reinstall.
