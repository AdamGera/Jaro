# Sessions and Meetings

A Meeting is a Session with call metadata and meeting controls. Both use the same recording workspace and transcript display on macOS, Windows, Linux, and the local browser.

| Layer | Shared behavior |
| --- | --- |
| Audio | Capture through the platform adapter. Keep the original tracks, and use the synchronized mix for transcription and speaker labels when both sources are present. |
| Live transcript | Decode audio with Rumble2 Flash and use a common timestamped transcript format. Native captures save complete preview chunks to a rolling transcript. The browser can stream preview audio from its microphone. |
| Finalization | Keep readable preview text while the final pass runs. Save one Session with its audio and final transcript, including Nemotron speaker labels when enabled. |
| Workspace | Reuse the same turn grouping, speaker bubbles, pause dividers, search, playback, notes, and overview. Show changes made by finalization without reopening the Session. |

Meeting-only behavior belongs in metadata, policy, or controls. Examples include call detection, automatic titles, transcript editing where available, exports, and retention. Existing recordings with **You** and **Them** speaker names keep those labels. Live transcript visibility does not disable the meeting excerpt used by Copy New and Copy All.

For new recording features, apply shared changes at the lowest layer that owns them. Add platform code where capture or permissions differ, and read meeting metadata only for behavior specific to meetings. Check both Session and Meeting flows, including a microphone-only Session, a Meeting with both inputs, toggling Live, processing after Stop, recovery, playback, and transcript search. Use the same workspace and data contract when adding support to another desktop build.

iOS and Android currently use their native keyboards for dictation. If either app adds a recording workspace, adopt the same Session and Meeting contract and use the platform's capture adapter.
