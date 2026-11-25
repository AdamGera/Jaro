# Contributing to Jaro

Use [GitHub Issues](https://github.com/AdamGera/Jaro/issues) for bugs and feature ideas. For a bug, include the operating system and app versions, steps to reproduce, and what you expected. A synthetic recording or a short, redacted log excerpt is enough for most reports. Use the [security policy](SECURITY.md) for vulnerability reports.

## Find the code

| Directory | What's inside |
| --- | --- |
| [engine/](engine/) | Python audio engine, HTTP service, local models, session storage, and CLI |
| [src/](src/) | React workspace shared by the desktop apps and the local browser |
| [desktop/](desktop/) | Native desktop hosts, platform capture helpers, installers, and integration fixtures |
| [mobile/](mobile/) | Native iOS and Android keyboards, offline dictation, and their build and release guides |
| [sdk/](sdk/) | TypeScript integration with the local transcription service |
| [tests/](tests/) | Python unit tests |
| [desktop/tests/](desktop/tests/) | Native, bridge, and WebKit integration fixtures |
| [tools/](tools/) | Tools for regenerating documentation media and runtime catalogs |

## Build and check

Implement new recording features in the layers shared by **New session** and **Record Meeting**. Use the [Sessions and Meetings architecture and verification checklist](docs/recording-workspace.md) for changes to capture, transcription, storage, the workspace, or a desktop port.

Use Apple Silicon macOS 26 or later for native builds. For the Python and web workspace, install Python 3.13 through uv, Bun, and the FFmpeg tools. Run these commands from the repository root:

```sh
uv sync --locked --python 3.13
bun install --frozen-lockfile
bun run build
PYTHONPATH=engine uv run --locked python -m unittest discover -s tests
node --test sdk/typescript/index.test.mjs
```

The Python unit suite uses temporary libraries and can run without downloading models. To exercise the engine with test audio after installing the models, run `uv run --locked python desktop/tests/bridge_smoke.py`. The bridge fixture allocates a separate library and loopback port.

In Finder, double-click **Setup Jaro.command** for source setup, then **Start Jaro.command** to launch. To start the source engine without opening a browser, run `uv run --locked python engine/server.py --ensure`. To use the source CLI, run `uv run --locked python engine/jardctl.py --help`.

See the [macOS build guide](desktop/build-tools/README.md) for the signed bundle, development toolchain, and isolated package smoke tests. See the [roadmap](ROADMAP.md) for validation still in progress.
