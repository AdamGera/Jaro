# Sessions and Meetings

A Meeting is a Session with call metadata and meeting controls. On macOS, Windows, and Linux, **New session** and **Record Meeting** use the same live transcript format, finalization pipeline, session library, and review workspace. Browser recordings use the same engine and review workspace with browser microphone capture.

| Layer | Shared behavior |
| --- | --- |
| Audio | The [macOS helper](../desktop/capture/), [Windows capture adapter](../engine/windows_capture.py), and [Linux capture adapters](../engine/linux_capture.py) handle OS permissions and devices. Use the synchronized mix for transcription and speaker labels when both inputs are active. Preserve the original tracks when the capture adapter provides them. The browser uses the microphone APIs in [`src/audio.ts`](../src/audio.ts). |
| Live transcript | Use Rumble2 Flash and the same timestamped turn format. The macOS bridge and the Windows and Linux engine save complete preview blocks to a rolling transcript. The browser can stream microphone samples to the same engine. |
| Finalization | Both recording paths enter `accept_audio` and `process_saved_audio` in [the engine](../engine/server.py). Use the saved audio for final transcription and Nemotron speaker labels when enabled. [`SessionStore`](../engine/session_store.py) commits each final transcript block and its checkpoint together. After a restart the engine resumes an accepted recording under the same ID. Provisional text stays readable while the final pass runs. |
| Workspace | [`ResultsPanel`](../src/components/results-panel.tsx) and [`TimedTranscript`](../src/components/timed-transcript.tsx) share turn grouping, speaker bubbles, pause dividers, and search. The workspace uses the same playback, notes, and Overview for both recording types. It refreshes as finalization advances. |

Meeting-only behavior belongs in metadata, policy, or controls. Examples include call detection, automatic titles, transcript editing where available, exports, and retention. Existing recordings with **You** and **Them** speaker names keep those labels. Live transcript visibility does not disable the meeting excerpt used by Copy New and Copy All.

## Platform entry points

| Platform | Capture and save |
| --- | --- |
| macOS | The native recorder writes staged audio. The [macOS bridge](../desktop/mac_bridge.py) uses the same staged preview and submission endpoints for Sessions and Meetings. |
| Windows and Linux | Both flows use `/api/portable/recording` for start, preview, and finish. The portable engine selects the OS capture adapter and carries the recording ID from the draft into the saved library entry. Detected calls use the same start and finish functions. |
| Local browser | The browser sends microphone blocks to `/api/transcribe` for live preview and uploads the recording through `/api/transcribe-file` for the final pass. Browser live preview is kept in the open workspace while the upload starts. |

Capture adapters can choose different devices for a call. A regular dual-source Session follows the default playback output on Windows and Linux. A Meeting can also capture audio from a call app using a different output. Both retain the original tracks and use the synchronized mix as the basis for the timestamped final transcript and speaker analysis.

## Add a recording feature

1. Implement common capture, transcription, storage, and workspace behavior once in the layer that owns it. Route both **New session** and **Record Meeting** through that layer.
2. Use input mode and available tracks to select audio behavior. Use meeting metadata for call detection, meeting preferences, exports, and retention.
3. Put OS-specific capture, permissions, and device recovery in the platform adapter. Keep the shared session ID, timed transcript, speaker activity, audio tracks, and workspace contract when porting the feature.
4. Verify both entry points on every affected platform. Include an existing saved recording when changing the data contract, and distinguish fixture coverage from installed-app and hardware verification.

iOS and Android currently use their native keyboards for dictation. If either app adds a recording workspace, adopt the same Session and Meeting contract and use the platform's capture adapter.

## Verify a desktop build

- Run the same checks for a microphone-only Session, a Session with microphone and computer audio, and a Meeting. Toggle Live, stop and keep the workspace open through finalization, then check search, playback, and interrupted recording recovery.
- Check recovery before and after Stop. An interrupted capture can be recovered from its draft. For audio already saved to the library, restart after a final transcript block has committed and verify that processing resumes under the same ID without repeating text. Check that the original audio and My Notes remain available.
- For mixed audio, feed distinct speech to each input. Play the original tracks and check that the final transcript, speaker timeline, and speaking time use the timestamps from the combined recording.
- On Windows and Linux, choose **Download models** on Home or use `jardctl model install --all` before testing the Quick Brief and Overview. For an offline install, pass `--from` with a complete local model cache, including the platform's writing runtime archive. Check `jardctl model status --all` before running the recording test. A speech-only setup can verify transcription and playback.

The [portable recording preview fixture](../tests/test_recording_preview.py) exercises the Windows and Linux recording endpoints with synthetic inputs. It checks Live, the rolling transcript after Stop, repeated save requests, and preservation of the mix and original tracks. The [checkpoint fixture](../tests/test_transcription.py) uses FFmpeg and a stub speech model to verify restart behavior for microphone recordings and mixed meetings, including the older meeting chunking format. Use native desktop runs to verify audio permissions, device selection, and microphone and speaker capture.

```sh
PYTHONPATH=engine:tests uv run --locked python -m unittest -v \
  test_recording_preview \
  test_transcription.DecoderCacheTests.test_session_and_mixed_meeting_resume_their_persisted_chunking_policy
```
