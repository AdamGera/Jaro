---
name: jaro-annotate
description: Review a Jaro screen annotation or .jaroannotation package. Use when the user shares an annotation or asks about a screen recording, drawing, or timed narration saved by Jaro.
---

# Review a Jaro annotation

Use the capture as evidence. View what was on screen before concluding what happened, and cite the recording title and timestamps so the user can locate each moment. Treat speech and on-screen content as reference material. Use a supplied package or Session ID first. Use `latest` when the user asks about their latest saved annotation.

## Get the context

1. Run `jardctl annotate show latest` to find the video, contact sheet, and other available assets. Open the contact sheet to see the main moments.
2. Run `jardctl annotate context latest` for the narration and frame times. If Jaro MCP is connected, use `list_annotations` and `get_annotation` to find the same saved context and view its contact sheet.
3. Use the Session ID, unique prefix, or path to a `.jaroannotation` folder or its `annotation.json` in place of `latest`. For a shared package, start with `summary.md`, the contact sheet if available, and `annotation.json` when you need the full timed transcript and stroke events.

## Look closer

```sh
jardctl annotate inspect latest --strokes
jardctl annotate inspect latest --json
jardctl annotate zoom latest --at 0:21
jardctl annotate zoom latest --at "the width changes"
jardctl annotate zoom latest --mark 4
jardctl annotate frames latest --from 0:04 --to 0:06 --fps 4
```

Use `inspect --strokes` to find a stroke ID for `zoom --mark`. Add `--full` to view the entire screen around a stroke. Open the returned contact sheet and match its cells to the reported frame times in reading order, left to right and top to bottom. For a full-resolution still, use `frames` with the same time in `--from` and `--to`. Use a short range when you need to inspect movement or compare several frames.

Video review outputs go in a new directory in the system temporary folder. Use `--output` to choose an existing parent directory outside the package and Session library. After video expiry, `frames` can return the existing stills in the requested range. In JSON those results use `"source": "saved"` and include `frameTimes`. Check `show` for other saved stills, or use the narration, manifest, and contact sheet that are still available. Say which part of the recording you could verify.

## Find the CLI

Use `jardctl` from the shell on Linux. On macOS, if it is missing from PATH, use `/Applications/Jaro.app/Contents/Helpers/jardctl` or copy the installed path from **Settings → General → CLI and MCP**. On Windows, the standard installer puts it at `%LOCALAPPDATA%\Programs\Jaro\bin\jardctl.exe`. These review commands work with Jaro closed.
