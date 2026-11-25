# Windows x64 desktop package

This installer targets Windows 11 x64. It installs for the current user. Jaro uses the system Microsoft Edge WebView2 Evergreen runtime. Install or repair WebView2 if the launcher reports it missing. [WebView2 distribution](https://learn.microsoft.com/microsoft-edge/webview2/concepts/distribution)

Install the latest Microsoft Visual C++ x64 Redistributable for the speech runtime. [Microsoft Visual C++ downloads](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist)

Build the web workspace with `bun install --frozen-lockfile` and `bun run build`. Use Native SDK CLI 0.11.1 and Zig 0.16.0 for the desktop host.

```sh
python desktop/portable/build.py windows
# Run the codec build on Linux with MinGW x64 C and C++, Meson, Ninja, pkg-config, make, and xz.
bash desktop/build-tools/build-codecs.sh "$PWD/desktop/out/portable/windows/codecs" windows
python desktop/portable/windows/package.py \
  --host desktop/out/portable/windows/bin \
  --sdk "$NATIVE_SDK_PATH" \
  --python-archive /path/to/python-3.13.17-embed-amd64.zip \
  --codecs desktop/out/portable/windows/codecs/ffmpeg-7.1.1 \
  --out desktop/out/portable/windows/installer
```

`NATIVE_SDK_PATH` points to the installed `@native-sdk/cli` package directory. Use a build Python with pip for staging. The builder verifies the upstream CPython archive, targets pinned Windows CPU wheels, and accepts `--wheelhouse /path/to/wheels` for offline builds. [CPython 3.13.17](https://www.python.org/downloads/release/python-31317/)

Run Inno Setup 7.1.1's `ISCC.exe` with the staged `setup.iss` path to produce the installer in the stage's `output` folder. [Inno Setup compiler](https://jrsoftware.org/inno-setup/documentation/topic_compilercmdline.htm)

Open Jaro from the Start menu and choose **Download models** on Home. For an offline install from a pinned model cache, run `jardctl model install --all --from` followed by the cache path. The CLI lives at `%LOCALAPPDATA%\Programs\Jaro\bin\jardctl.exe`.

If you installed Rumble2 Flash alone, use **Settings → General → Local models** in the desktop app to add speaker labels, word timing, and writing support. Speech remains available during setup.

If the installed models fail to start, choose **Repair models**. Jaro checks the pinned files and reuses verified copies. You can also quit Jaro and run `jardctl model install --all --repair`. Add `--from` followed by the cache path to repair from an offline cache.

Opening Jaro again brings its running window forward, including when it is minimized. The running engine keeps ownership of the library and any active recording.

**Settings → General → Application → Launch at login** registers Jaro to start in the background when you sign in. It starts off. If you already enabled a dictation shortcut and installed the speech model, dictation can be ready before you open the workspace. Open Jaro from Start to show the workspace. If Windows has turned off the startup entry, use **Open Startup Settings** in Jaro to reach **Settings → Apps → Startup**, then turn on Jaro there.

Close the workspace to keep Jaro in the notification area when the tray is available. From its menu you can open Jaro, record or save a meeting, copy the last transcript, or quit. Closing the workspace exits if Windows cannot register the tray.

Open **Meetings → Record Meeting** to capture your microphone and computer audio across active playback devices, including a headset selected by the call app. Use **View live transcript** during capture and **Stop & Save** when finished. The final transcript uses Rumble2 Flash and Nemotron on the mix. The session keeps the microphone and computer audio tracks and offers Markdown and subtitle exports. Interrupted meetings can be recovered from Home.

Use **New session** for a regular recording. Sessions and Meetings share the same workspace, and saved recordings resume transcription after a restart from their last committed checkpoint. See the [shared recording workflow](../../../docs/recording-workspace.md).

In **Settings → Meetings**, control local Qwen titles and summaries, meeting retention, audio retention, and optional microphone processing. StuPASE and microphone boost start off. Processing changes the audio used for recognition and keeps the original audio available for playback. Jaro uses the Recycle Bin for expired meetings and retains the archive locally while a transfer is pending.

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
then `jardctl meetings delete <uuid>` to move a settled meeting to the Recycle Bin.
Add `--json` to check whether delivery is pending.

Under **Detected calls**, turn on offers and choose Ask, Always record, or Never offer per supported app or site. The desktop panel can offer to record, stop and save, or keep recording even while the workspace is closed and the login engine is running. Starting from the desktop uses automatic speech language detection.

Open **Options** from a recording offer to choose **Never offer** for that app or site. When **Always record** is available, it starts this recording on request and saves the rule for future calls. Opening or canceling the options leaves the policy alone.

While a supported browser holds the microphone, Jaro checks its visible windows for call sites such as Google Meet and Microsoft Teams. A recognized address can use the site's **Always record** setting. A title-only match needs confirmation, and an unidentified call uses the browser's Ask or Never offer setting. Web Teams shares the desktop Teams policy. Jaro only saves the service name with the meeting.

When you record a detected call, Jaro follows the playback devices used by the process holding the call microphone. If that process only records, Jaro follows the other audio processes from the same app or browser. If Windows cannot identify or open any of those outputs, Jaro can fall back to the available playback devices. Manual meetings continue to capture across active playback devices.

When **Stop when the call ends** is enabled, automatically started meetings keep recording while the call app uses the microphone or plays audio. When both stop, Jaro gives you 30 seconds to use **Keep recording**. A confirmed switch to another Google Meet room can also start the countdown. Resuming call audio after a quiet period or returning to the recorded room cancels the associated countdown.

Use **Settings → Dictation → Voice input** to choose a dictation microphone. Jaro follows the system default until you select one, and falls back to the system default for a take if your selected microphone is unavailable at startup.

**Noise cleanup (StuPASE)** and **Boost microphone (+6 dB)** are independent and both start off. Cleanup downloads 2.3 GB of model weights on first use and runs locally before transcription. Boost applies a peak limiter and stays active if cleanup is unavailable. These settings apply to system-wide dictation, and History keeps the original microphone audio.

Choose **Top**, **Bottom**, or **Off** under **Settings → Dictation → Feedback** to place the recording indicator. It stays above the focused app while Jaro listens or processes speech and lets clicks reach the window underneath.

Use the same section to mute dictation cues, choose a sound style, preview its start sound, or set cue volume. Jaro uses the default playback device.

Under **While dictating**, choose whether to leave playback alone, mute or lower other apps already playing on the default output, or pause compatible media players. Jaro resumes the players it paused if they are still paused on the same item.

Jaro asks Windows to prevent idle sleep while it records dictation, audio, or video. When recording ends, Jaro releases the request and Windows resumes its usual power policy.

Press **Escape** to discard a hands-free take or cancel a take that is still processing. **Cancel dictation** in **Dictation → General** is also available while Jaro starts, listens, or processes the take.

Add names and alternate spellings with **Vocabulary** under **Voice input**. Enter whole-phrase rules such as `kitchen table => studio desk` under **Dictation → Output** or **Settings → Dictation**. Rules apply to new system dictations and can hold up to 4,000 characters. History shows the corrected text and keeps the original audio.

In **Dictation → Output**, assign **Dictate without processing** to a separate shortcut. It starts unassigned and uses your current shortcut mode. The original Rumble2 Flash transcript goes to the active field and appears as **Raw** in History. **Paste Last** inserts that text verbatim.

Use **Settings → Dictation → Shortcuts** to choose how Left Alt controls dictation. **Hold** is the default. Choose **Tap to start and stop**, **Hold or double-tap**, or **Double-tap only** for hands-free dictation. Tap Left Alt to finish a hands-free take. If the destination field changes, use **Copy last** to recover the transcript.

Enable the shortcut in **Dictation → General**. Jaro remembers your choice and reconnects after restart once the speech model is ready. If the listener cannot start, choose **Enable shortcut** to retry or **Disable on launch** to clear the saved preference.

When the global shortcut ends a take, Jaro keeps 200 ms of audio for the last syllable. If you're still speaking, it waits for a pause for up to 600 ms total. Stopping from the workspace ends capture immediately.

If the microphone stops delivering audio mid-dictation, Jaro transcribes what it heard and copies the text to the clipboard. You can review it in **Dictation → History** when History is enabled, or use **Copy last** if the clipboard was unavailable.

You can start another dictation while Jaro processes the first. Results finish in order, and paste waits until the microphone closes. If focus leaves a take's original field, that transcript stays available in History when enabled.

Back-to-back dictations in the same field join with a space and sentence-aware capitalization. **Paste Last** can continue the same text. Typing, clicking, or switching focus ends the join. History keeps each take's own transcript.

Windows can block automatic paste into an app running as administrator. When Jaro detects this, Dictation offers **Copy last** so you can paste manually, even with History off. [Windows input security](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)

In the same section, choose **Language** to give Rumble2 Flash a spoken-language hint for system-wide dictation. **Detect automatically** is the default.

With Dictation History on, open **Dictation → History** and select an entry to play its original microphone audio for up to 24 hours. Dictation text follows your History retention setting. Deleting an entry or clearing History also deletes its audio.

Use `jardctl history --text` to read retained dictations or `jardctl history clear --yes` to clear them and reset Paste Last. Clear also resets Paste Last when History is off.

With Jaro running and the dictation shortcut enabled, use `jardctl dictation start`, `stop`, `toggle`, `cancel`, or `paste-last`. Pass `--raw` to `start` or `toggle` to skip output processing. `jardctl dictation stop --print` sends the transcript to stdout and saves it as **Printed** when History is on. It leaves the focused field, clipboard, and Paste Last unchanged. Use `--json` for structured output. A silent take exits with code 4.

On Home, open **Record screen** and turn on **Microphone narration** or **Computer audio** to include those inputs. For narration, keep **System default** or choose a microphone for the recording. If that microphone is disconnected before you start, choose another input. Pick a **Display** or **Window** to record. With both audio sources enabled, Jaro saves the two sources and their mix in the MP4 and uses the mix for local transcription. Choose **Stop and save** to open the recording in Sessions. If Jaro closes during capture, reopen it and choose **Recover** under **Interrupted screen recording** for the video and audio that reached disk. [Windows Graphics Capture](https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture)

If an audio source disconnects during screen recording, Jaro keeps recording and tries to reconnect it. That source’s track is silent during the gap. A selected microphone keeps its own track when it returns.

In **Settings → Annotate**, enable the global shortcut. Press **Ctrl+Alt+Shift+F10** to record the display under the pointer with microphone narration. Tap it again or click **Stop & Save** on the recording control. Hold the shortcut during a recording to draw on the display, then release it to keep recording. You can choose a different shortcut independently of dictation.

With Jaro running, `jardctl annotate start` records the display under the pointer with microphone narration. Use `jardctl annotate targets --source window` to list window IDs, then run `jardctl annotate start --source window --target <id>`. For an area, use `--source area` and select it in Jaro. Run `jardctl annotate status`, `jardctl annotate stop`, or `jardctl annotate cancel` to check, save, or discard the active capture. Bind `jardctl annotate toggle` to one key to start the default display capture and press it again to save. A second press while the picker is open leaves the picker open.

Choose **Area** to record part of a display. Select the display, drag on its preview, and choose **Record area**. **Refresh preview** gives you three seconds to switch windows. Jaro records the selected area in display pixels.

During a display or area recording, choose **Draw on screen** to mark the shared desktop. Press Esc or right-click inside the drawing area to finish drawing while the recording continues. Jaro uses physical display pixels to place the pen and adds the timed strokes when it saves the video.

Use **Draw** for window recordings or when the desktop pen is unavailable. **Done drawing** closes the preview. Strokes fade after you lift the pointer and appear in the saved MP4. Completed strokes stay with an interrupted recording and are included when you recover it.

Open **Images** in the saved recording’s **Overview** to review sampled frames. Select a timestamp to jump in the video or choose **Save contact sheet** to download the PNG.

In Jaro’s desktop window, choose **Save video…** for a full MP4 copy, or choose **Trim video**, set the range, and preview it. **Save clip…** opens the system save dialog. The trimmed copy includes the drawings and all saved audio tracks.

In a saved session’s **Overview**, open **Add images** to capture a window, display, or area. Display screenshots wait three seconds so you can switch windows. For an area screenshot, select and save a crop of the preview. The image appears in that session’s gallery.

In the Jaro desktop window, open **Send to your agent** beside **Images**. Choose Claude Code, Codex, OpenCode, or T3 Code. Select an existing chat and choose **Send context** to share the saved overview, transcript, My Notes, and gallery images.

Install and sign in to the agent you use locally. OpenCode uses its running v2 service. Claude Code, Codex, and T3 Code use their Windows command-line tools. T3 Code receives paths to image files on this computer. For screen annotations sent to OpenCode, **OpenCode deliveries** shows the receipt and any available Undo or correction action.

When you stop an annotation from the desktop shortcut or CLI, Jaro can identify the active app or a verified browser site. In the OpenCode picker, select a chat and choose **Remember destination** to suggest it for future matching annotations. Confirm suggested sends with **Send context**. Choose **Forget** to remove the suggestion.

Open **Settings → Annotate → Manage rules → Add rule** to set up a suggestion before recording. Enter a website domain, a project path such as `github.com/org/repo`, or an app’s executable name such as `ReviewPad.exe`. Then choose an OpenCode chat. Paths match at folder boundaries, and the most specific path wins. Shared services such as GitHub require a project path. Use **Change chat** to update an existing rule. You can remove a suggestion even when OpenCode is closed.

Select **Send automatically** on a saved rule to send future matching annotations. Jaro shows the chosen chat and a three-second countdown after saving the narration. Hover to pause or choose **Cancel send** to keep the annotation in Sessions. Sending requires a visible countdown panel. Run `jardctl annotate route ID` to check its state or add `--cancel` to cancel a pending send.

After OpenCode queues the annotation, the desktop panel offers **Undo send** for five seconds. Hover to keep the button available. The delivery receipt in Sessions and `jardctl annotate undo RECEIPT` can withdraw the annotation while it is still queued, for up to ten minutes.

Choose **Terminal folder** and enter a local drive path such as `C:\work\project`. Stop the annotation with `jardctl annotate stop --from-project` from a Windows shell in that folder or one of its subfolders. Jaro uses the working folder to suggest a chat, and you confirm the send in the gallery.

Choose **Project’s newest chat** to follow the most recently updated open chat in a project, including related Git worktrees. Enter an absolute path as OpenCode sees it. Use a drive path such as `C:\work\project` for native OpenCode or a Linux path such as `/home/user/project` for OpenCode in WSL.

To use OpenCode in your default WSL 2 distribution, turn on **OpenCode in WSL** in **Settings → Annotate → Agent destinations**. The distribution needs `/usr/bin/python3`. When the WSL service is unavailable, the picker can show a native Windows OpenCode service. If you already selected a WSL chat or have a WSL delivery receipt, that action stays tied to its original service. [Microsoft’s WSL commands](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)

From the CLI, use `jardctl annotate destinations` to get the chat and service IDs. Run `jardctl annotate send latest --to SESSION --service SERVICE` to send the latest annotation’s narration and contact sheet. Use `jardctl annotate deliveries latest` to find the receipt. Run `jardctl annotate undo RECEIPT` to withdraw a queued delivery, or `jardctl annotate correct RECEIPT` to ask the chat to disregard a received copy.

Jaro checks for updates shortly after launch and hourly while it is open. Choose **Look for new versions** next to Settings or in **Settings → General** to check immediately. The notification area also offers **Check for Updates**, **Download Update**, or **Restart to Update** when available. Jaro prepares an in-app update when the installer matches its published GitHub digest and Windows validates its Authenticode signature. The verified publisher subject must match the installed launcher's publisher subject. **Restart to Update** confirms that the published installer still matches, waits for the workspace to close, runs Setup, and reopens Jaro. If automatic verification is unavailable, **Download Update** opens the installer download in your browser.

On builds that support startup verification, **Restart to Update** waits for the desktop workspace to connect. If the new launcher exits early or the window times out, Jaro restores the previous version and skips that installer on later checks. A corrected installer can be offered under the same version number.

If an in-app download is interrupted, choose **Look for new versions** to resume it. Jaro keeps the partial download when you quit.

Before closing the workspace, Jaro verifies that the staged installer still matches its published checksum. If the installer changed, choose **Look for new versions** to download a fresh copy.

Close the app and let CLI jobs finish before installing a newer package or uninstalling. The launchers and Setup use the same lock. Setup selects the new version after copying the complete payload. Windows Installed Apps can remove the installation. Sessions, preferences, and models stay under `%LOCALAPPDATA%\Jaro`.

Setup recreates the Jaro Start Menu entry. It also updates local Start Menu, Desktop, and taskbar shortcuts that point directly to a Jaro version folder. They keep their names, arguments, and custom icons, and the stable launcher follows the active version after an update or rollback.

The installer can be built unsigned for local tests. For release builds, sign the staged Jaro executables, including both stable launchers, with the Windows release certificate before compiling the installer.

## Sign a release installer

Compile the staged script with the pinned Inno Setup and pass `SignedEngineDir` as an existing directory.

```powershell
& $iscc "/DSignedEngineDir=$signedEngineDir" $stagedSetupScript
```

The first compile for a given engine stops and writes the executable to sign into that directory. Sign that file in place with the release certificate, then repeat the same compile. Inno Setup embeds that signature in the temporary Setup engine and the installed uninstaller. Sign the resulting installer with the same publisher and verify its Authenticode signature before uploading it. Run the install, update, and uninstall checks on a clean Windows 11 x64 machine with the signed artifacts. [Inno Setup signed uninstaller](https://jrsoftware.org/inno-setup/documentation/topic_setup_signeduninstaller.htm)

Jaro keeps unsaved workspace drafts and its recording source and sidebar preferences in the local library. The default WebView2 data folder is the library’s `.webview2` directory. An explicit `WEBVIEW2_USER_DATA_FOLDER` override still applies.
