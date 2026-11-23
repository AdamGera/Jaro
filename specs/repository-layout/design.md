# Repository layout design

Hex's root presents the product in README, then links to CONTRIBUTING, SECURITY, ROADMAP, and a documentation index. Source, tests, packaging, and scripts live in dedicated directories. Jard uses those boundaries with the existing web, native macOS, and SDK toolchains.

- `engine/` holds the Python modules, CLI entry point, and runtime catalogs. `tests/` holds Python unit tests. The existing `src/`, `desktop/`, `sdk/`, and `tools/` remain the web, macOS, integration, and maintenance entry points.
- Source Python runs by script path for the engine and CLI, or with `PYTHONPATH=engine` for module imports and unit tests. The source bridge adds the checkout's engine directory. The packaged bridge still imports from its containing engine directory.
- Model and built web asset lookup select the checkout root in source builds and the existing engine resources in the bundle. Existing Finder entry files stay at the root because the launcher and welcome page locate them by name.
- The packager copies its explicit allowlist from the new source directory into the existing signed bundle layout. The frontend and engine read the same language catalog.
- The documentation index links to the user guide, build guide, SDK, and parity spec. The roadmap summarizes current coverage and points to the verification checklist for the remaining work.

Verify Python discovery and the web build from the root, source launch and bridge behavior with a disposable library, source SDK tests, and a signed package smoke test.
