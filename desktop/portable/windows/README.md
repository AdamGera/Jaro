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

Opening Jaro again brings its running window forward, including when it is minimized. The running engine keeps ownership of the library and any active recording.

**Settings → General → Application → Launch at login** registers Jaro to start in the background when you sign in. It starts off. If you already enabled a dictation shortcut and installed the speech model, dictation can be ready before you open the workspace. Open Jaro from Start to show the workspace. Windows can also manage this registration under **Settings → Apps → Startup**.

Close the workspace to keep Jaro in the notification area when the tray is available. From its menu you can open Jaro, record or save a meeting, copy the last transcript, or quit. Closing the workspace exits if Windows cannot register the tray.

Open **Meetings → Record Meeting** to capture your microphone and computer audio across active playback devices, including a headset selected by the call app. Use **View live transcript** during capture and **Stop & Save** when finished. The final transcript uses Rumble2 Flash and Nemotron on the mix. The session keeps the microphone and computer audio tracks and offers Markdown and subtitle exports. Interrupted meetings can be recovered from Home.

In **Settings → Meetings**, control local Qwen titles and summaries, meeting retention, audio retention, and optional microphone processing. StuPASE and microphone boost start off. Processing changes the audio used for recognition and keeps the original audio available for playback. Jaro uses the Recycle Bin for expired meetings and retains the archive locally while a transfer is pending.

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

Press **Escape** to discard a hands-free take, or choose **Cancel dictation** from **Dictation → General** while Jaro is starting, listening, or processing.

Add names and alternate spellings with **Vocabulary** under **Voice input**. Enter whole-phrase rules such as `kitchen table => studio desk` under **Dictation → Output** or **Settings → Dictation**. Rules apply to new system dictations and can hold up to 4,000 characters. History shows the corrected text and keeps the original audio.

Use **Settings → Dictation → Shortcuts** to choose how Left Alt controls dictation. **Hold** is the default. Choose **Tap to start and stop**, **Hold or double-tap**, or **Double-tap only** for hands-free dictation. Tap Left Alt to finish a hands-free take, or press Escape to discard it. If the destination field changes, use **Copy last** to recover the transcript.

Enable the shortcut in **Dictation → General**. Jaro remembers your choice and reconnects after restart once the speech model is ready. If the listener cannot start, choose **Enable shortcut** to retry or **Disable on launch** to clear the saved preference.

When the global shortcut ends a take, Jaro keeps 200 ms of audio for the last syllable. If you're still speaking, it waits for a pause for up to 600 ms total. Stopping from the workspace ends capture immediately.

If the microphone stops delivering audio mid-dictation, Jaro transcribes what it heard and copies the text to the clipboard. You can review it in **Dictation → History** when History is enabled, or use **Copy last** if the clipboard was unavailable.

You can start another dictation while Jaro processes the first. Results finish in order, and paste waits until the microphone closes. If focus leaves a take's original field, that transcript stays available in History when enabled.

Back-to-back dictations in the same field join with a space and sentence-aware capitalization. **Paste Last** can continue the same text. Typing, clicking, or switching focus ends the join. History keeps each take's own transcript.

In the same section, choose **Language** to give Rumble2 Flash a spoken-language hint for system-wide dictation. **Detect automatically** is the default.

With Dictation History on, open **Dictation → History** and select an entry to play its original microphone audio for up to 24 hours. Dictation text follows your History retention setting. Deleting an entry or clearing History also deletes its audio.

On Home, open **Record screen**, choose **Display** or **Window**, and select a source. Turn on **Microphone narration** or **Computer audio** before picking a source to include those inputs. With both enabled, Jaro saves the two sources and their mix in the MP4 and uses the mix for local transcription. Choose **Stop and save** to open the recording in Sessions. If Jaro closes during capture, reopen it and choose **Recover** under **Interrupted screen recording** for the video and audio that reached disk. [Windows Graphics Capture](https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture)

Choose **Area** to record part of a display. Select the display, drag on its preview, and choose **Record area**. **Refresh preview** gives you three seconds to switch windows. Jaro records the selected area in display pixels.

During a display or area recording, choose **Draw on screen** to mark the shared desktop. Press Esc or right-click inside the drawing area to finish drawing while the recording continues. Jaro uses physical display pixels to place the pen and adds the timed strokes when it saves the video.

Use **Draw** for window recordings or when the desktop pen is unavailable. **Done drawing** closes the preview. Strokes fade after you lift the pointer and appear in the saved MP4. Completed strokes stay with an interrupted recording and are included when you recover it.

In a saved session’s **Overview**, open **Add images** to capture a window, display, or area. Display screenshots wait three seconds so you can switch windows. For an area screenshot, select and save a crop of the preview. The image appears in that session’s gallery.

Jaro checks for updates shortly after launch and hourly while it is open. Choose **Look for new versions** next to Settings or in **Settings → General** to check immediately. Jaro prepares an in-app update when the published installer matches its GitHub digest and Windows verifies it with the same signing certificate as the installed launcher. **Restart to Update** confirms that the published installer still matches, waits for the workspace to close, runs Setup, and reopens Jaro. If automatic verification is unavailable, **Download Update** opens the installer download in your browser.

Close the app and let CLI jobs finish before installing a newer package or uninstalling. The launchers and Setup use the same lock. Setup selects the new version after copying the complete payload. Windows Installed Apps can remove the installation. Sessions, preferences, and models stay under `%LOCALAPPDATA%\Jaro`.

This is an unsigned test installer. Verify the Windows 11 setup and update flow on a clean machine and sign the launchers, setup engine, uninstaller, and installer before publishing a download.
