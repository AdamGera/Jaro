# Repository layout tasks

- [x] 1. Move engine modules and catalogs to `engine/` and unit tests to `tests/`. Update source invocations, runtime path resolution, the frontend import, and the packager allowlist. _Requirements 1.1, 2.1–2.2_
- [x] 2. Document the layout, contributing checks, security reporting, and current roadmap. Link the guides from the README. _Requirement 1.2_
- [x] 3. Verify Python discovery, web and SDK checks, the source bridge, and the signed bundle. The unit suite matched the baseline at 172 tests with two skips. The source bridge ran Rumble2 Flash and Qwen with a disposable library. The signed package passed its smoke test and retained the installed app and capture helper signing requirements. _Requirements 2.1–2.3_
