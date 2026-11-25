# Security

For a suspected vulnerability, use [GitHub private reporting](https://github.com/AdamGera/Jaro/security/advisories/new) if the repository offers it. If that form is unavailable, contact [Jard](https://www.jard.ai/) to arrange private reporting.

Include the Jaro build number, operating system and version, and a reproducible example with synthetic data. For keyboard reports, include the device model and the app or field type where the issue occurred. Keep recordings, transcripts, unredacted logs, local service tokens, and signing keys out of public issues.

## Trust boundaries

Jaro processes recordings and notes locally. First-run model setup and update checks contact their configured download services. The desktop bridge and transcription service use authenticated local control paths. The updater verifies its signed release feed and the candidate bundle before replacement.

The mobile keyboards process speech on the device. The [mobile privacy guide](mobile/PRIVACY.md) describes microphone access, iOS Full Access, local typing data, and optional model downloads.

The shared macOS engine requires a private credential for session data, media, and recording operations. Opening Jaro or choosing **Open in Browser** authorizes that browser session. The workspace cookie grants workspace access. Native controls and the SDK use separate credentials.

Optional commands and integrations can run code or send content with the user's permissions. Review the commands and destinations you configure.

Dictation Output matches a selected executable’s path and contents to its approved version before running a private copy. This approval covers the selected file. Its interpreters, libraries, and child programs retain their usual access.

A transformation catalog includes its manifest, executable entries, and declared dependencies in the approval. Named steps start from a private copy of the declared files with a limited inherited environment and the user's normal macOS permissions.

An embedded JavaScript or TypeScript configuration runs in a bounded QuickJS process. Version review fingerprints the entry and all statically imported local modules. Configuration loading has no transcript or network and program permissions. During a transformation, the desktop bridge permits HTTP requests and programs only with the corresponding grants for that version. The review shows destinations and program names it can identify. A grant covers the full capability, including destinations selected at runtime. Revocation cancels outstanding requests and ends process groups owned by the call.

An organization can force transformation restrictions through a macOS configuration profile. Under the loopback policy, the HTTP transport bypasses proxies and connects to numeric loopback addresses from the checked resolution. Every redirect follows the same rule.

Explicit transformation console messages from a manual check or approved run go to the user-owned local diagnostic log. Automatic records omit transcript and operation contents.
