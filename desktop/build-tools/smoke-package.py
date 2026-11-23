#!/usr/bin/env python3
"""Verify a packaged app with system PATH, temporary HOME, and a disposable session library."""
import argparse
import fcntl
from array import array
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import wave
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


def check_linked_libraries(app):
    # Wheel dylibs can retain their upstream install ID. Only a load command
    # creates a runtime dependency, so inspect those independently from the ID.
    from package import is_mach
    count = 0
    for path in app.rglob("*"):
        if not is_mach(path):
            continue
        count += 1
        commands = subprocess.check_output(["otool", "-l", path], text=True)
        for block in commands.split("Load command ")[1:]:
            lines = block.splitlines()
            fields = [line.strip() for line in lines]
            kind = next((line.split()[-1] for line in fields if line.startswith("cmd ")), "")
            if kind not in {"LC_LOAD_DYLIB", "LC_LOAD_WEAK_DYLIB", "LC_REEXPORT_DYLIB", "LC_LOAD_UPWARD_DYLIB"}:
                continue
            name = next(line[5:].split(" (offset ")[0] for line in fields if line.startswith("name "))
            assert name.startswith(("@", "/usr/lib/", "/System/Library/", "/Library/Apple/")), (path, name)
    print(f"Verified runtime library paths in {count} packaged Mach-O binaries.", flush=True)



def wait_ready(api):
    for _ in range(240):
        with api("/api/status") as response:
            status = json.load(response)
        if status["status"] != "loading":
            return status
        time.sleep(0.5)
    raise AssertionError("The packaged engine did not finish starting")


def stop_engine(api, base, library):
    with api("/api/quit", b"", {"Origin": base, "X-Jard-Action": "quit"}) as response:
        assert response.status == 202
    for _ in range(60):
        with (library / ".server.lock").open("rb") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                time.sleep(0.5)
            else:
                assert not (library / ".jard-sdk.json").exists()
                return
    raise AssertionError("The packaged engine did not release the library")


def verify_embedded_sdk(helpers, sdk_file, audio, library, work, env):
    scratch = work / "embedded"
    scratch.mkdir(mode=0o700)
    env = {**env, "TMPDIR": str(scratch)}
    database = library / "sessions.sqlite3"
    before = (set(library.rglob("*")), database.stat().st_mtime_ns)

    offline = subprocess.check_output([str(helpers / "jardctl"), "--json", "transcribe", str(audio), "--language", "en"],
                                      cwd=work, env=env, text=True, timeout=90)
    assert "library" in json.loads(offline)["text"].lower(), offline
    leftovers = list(scratch.iterdir())
    assert not leftovers, f"The offline CLI left temporary files: {leftovers}"
    print("Packaged CLI transcribed with the shared engine stopped.", flush=True)

    if node := shutil.which("node"):
        subprocess.run([node, "--input-type=module", "-", str(sdk_file), str(audio), str(library), str(scratch)],
                       cwd=work, env=env, check=True, text=True, timeout=140, input=r"""
import assert from "node:assert/strict"
import { readFile, readdir } from "node:fs/promises"
import { pathToFileURL } from "node:url"
const [modulePath, file, library, scratch] = process.argv.slice(2)
const { create } = await import(pathToFileURL(modulePath).href)
const first = await create({ library })
let second
try {
  second = await create({ library })
  assert.notEqual(first.pid, second.pid)
  const ownedDirectories = await readdir(scratch)
  assert.equal(ownedDirectories.length, 2)
  assert.ok(ownedDirectories.every(name => name.startsWith("jard-embedded-")))
  const audio = await readFile(file)
  const result = await first.transcribe(audio, { languages: ["en"] })
  assert.match(result.transcript, /library/i)
  assert.ok(result.durationMs > 1000 && result.durationMs < 15000)
  await first.close()
  assert.throws(() => process.kill(first.pid, 0), { code: "ESRCH" })
  assert.equal((await second.health()).status, "ready")
  assert.equal((await readdir(scratch)).length, 1)
  assert.match((await second.transcribe(audio, { languages: ["en"] })).transcript, /library/i)
} finally {
  await Promise.all([first.close(), second?.close()])
}
assert.throws(() => process.kill(second.pid, 0), { code: "ESRCH" })
assert.deepEqual(await readdir(scratch), [])
console.log("Two signed embedded helpers transcribed independently and exited on lease closure.")
""")
    assert not (library / ".jard-sdk.json").exists()
    assert before == (set(library.rglob("*")), database.stat().st_mtime_ns), "Embedded transcription changed the library."


def seed_pending_meetings(library, samples, echo, token, api):
    with api("/api/native/staging?recording=true&meeting=true&provider=Teams", b"", token) as response:
        stranded = json.load(response)
    prefix_frames = 25 * 48_000

    def wav(path, source, prefix=0):
        with wave.open(str(path), "wb") as stream:
            stream.setparams((1, 2, 48_000, 0, "NONE", "not compressed"))
            if prefix:
                stream.writeframesraw(bytes(prefix * 2))
            stream.writeframes(source.tobytes())

    mixed = Path(stranded["path"])
    # Simulate the mixed file from a recorder that quit before handing over its claim.
    for path, source in ((mixed, samples), (mixed.with_suffix(".mic.wav"), echo), (mixed.with_suffix(".system.wav"), samples)):
        wav(path, source)
    pending, exhausted = str(uuid4()), str(uuid4())
    duration = (prefix_frames + len(samples)) / 48_000
    for identifier in (pending, exhausted):
        folder = library / "audio" / identifier
        folder.mkdir()
        wav(folder / "source.wav", samples, prefix_frames)
        wav(folder / "mic.wav", echo, prefix_frames)
        wav(folder / "system.wav", samples, prefix_frames)

    options = {"recording": True, "diarization": True, "meeting": True, "sourceSpeakerTurns": True, "languageHints": ["en"],
               "meetingProvider": "Zoom"}
    checkpoint = {"text": "Checkpoint already saved.", "language": "English", "words": [],
                  "segments": [{"speaker": 2, "start": 0.5, "end": 2, "text": "Checkpoint already saved."}],
                  "meetingCoverage": {"1": [[0, 25]], "2": [[0, 25]]}}
    with closing(sqlite3.connect(library / "sessions.sqlite3", timeout=10)) as db, db:
        for identifier, attempts in ((pending, 1), (exhausted, 2)):
            db.execute("""INSERT INTO sessions(id,name,created,filename,mime,options,duration,processed,status,search_text)
                       VALUES(?, ?, '2024-05-04T00:00:00+00:00','source.wav','audio/wav',?, ?,25,'processing',?)""",
                       (identifier, "Resume fixture", json.dumps(options), duration, checkpoint["text"]))
            db.execute("INSERT INTO chunks VALUES(?,?,?)", (identifier, 25, json.dumps(checkpoint)))
            db.execute("INSERT INTO meeting_finalization VALUES(?,?,?)", (identifier, prefix_frames, attempts))
    return pending, exhausted, stranded["id"]


def verify_restart_recovery(library, helper, api, pending, exhausted, stranded):
    # Hold a fixture recorder claim while the restarted engine becomes ready.
    # Recovery must wait for it, then continue when the claim is released.
    active = library / "staging" / (str(uuid4()) + ".recording")
    with active.open("wb") as marker:
        fcntl.flock(marker, fcntl.LOCK_EX)
        helper("jard-engine", "start")
        assert wait_ready(api)["status"] == "ready"
        time.sleep(2.2)
        with api(f"/api/sessions/{pending}") as response:
            waiting = json.load(response)
        assert waiting["status"] == "interrupted" and waiting["finalizationPending"], waiting
    active.unlink()
    remaining = {pending, stranded}
    completed = {}
    for _ in range(120):
        for identifier in tuple(remaining):
            with api("/api/sessions?meetings=true") as response:
                meetings = json.load(response)
            if any(row["id"] == identifier and row["status"] == "complete" for row in meetings):
                with api(f"/api/sessions/{identifier}") as response:
                    completed[identifier] = json.load(response)
                remaining.remove(identifier)
        if not remaining:
            break
        time.sleep(0.5)
    assert not remaining, remaining
    resumed = completed[pending]
    assert not resumed["finalizationPending"] and "library" in resumed["transcript"]["text"].lower(), resumed
    assert resumed["transcript"]["text"].count("Checkpoint already saved.") == 1
    last = resumed["transcript"]["segments"][-1]
    assert (last["start"] >= 25 or any(word.get("start") and word["start"] >= 25 for word in resumed["transcript"]["words"])), resumed
    assert completed[stranded]["meetingProvider"] == "Teams" and not (library / "staging" / stranded).exists()
    with api(f"/api/sessions/{exhausted}") as response:
        failed = json.load(response)
    assert not failed["finalizationPending"] and failed["status"] == "interrupted", failed
    assert "after two attempts" in failed["transcript"]["interruption"]
    assert (library / "audio" / exhausted / "source.wav").is_file()
    print("Packaged restart recovered the checkpoint and a stranded take, and honored the retry and recorder guards.", flush=True)


def disposable_trash_location(library, helper):
    # Measure where Foundation sends disposable folders on this volume, then put the probe back.
    probe = library / ("Jard disposable Trash probe " + str(uuid4()))
    probe.mkdir()
    location = Path(json.loads(helper("jard-dialog", "trash", str(probe)))["path"])
    os.rename(location, probe)
    return location.parent


def verify_retention(library, helper, api, base, managed_meeting):
    trash = disposable_trash_location(library, helper)
    managed_id, transcript_file, notes_file = managed_meeting
    trashed_export = trash / transcript_file.name
    managed_archive = trash / f"Jard Meeting {managed_id}"
    transcript_contents = transcript_file.read_bytes()
    edited_notes = notes_file.read_bytes() + b"\nKeep this user edit.\n"
    notes_file.write_bytes(edited_notes)
    manual_copy = transcript_file.with_name("Manually saved transcript.md")
    manual_copy.write_bytes(transcript_contents)
    assert not trashed_export.exists() and not managed_archive.exists()
    identifier = str(uuid4())
    audio = library / "audio" / identifier
    audio.mkdir()
    (audio / "source.wav").write_bytes(b"disposable recording")
    retained = trash / f"Jard Meeting {identifier}"
    details = {"text": "Disposable transcript.", "durationSeconds": 1, "segments": [
        {"speaker": 2, "start": 0.1, "end": 0.9, "text": "Disposable transcript."}],
        "words": [], "speakerNames": {"2": "Them"}}
    with closing(sqlite3.connect(library / "sessions.sqlite3", timeout=10)) as db, db:
        db.execute("UPDATE sessions SET created=strftime('%Y-%m-%dT%H:%M:%SZ','now'), options=json_remove(options,'$.meetingEndedAt')")
        db.execute("UPDATE sessions SET created='2000-01-01T00:00:00+00:00' WHERE id=?", (managed_id,))
        db.execute("""INSERT INTO sessions(id,name,created,filename,mime,options,duration,processed,status,transcript,search_text)
                   VALUES(?, 'Disposable retention meeting','2000-01-01T00:00:00+00:00','source.wav','audio/wav',?,1,1,'complete',?,?)""",
                   (identifier, json.dumps({"recording": True, "diarization": False, "meeting": True}),
                    json.dumps(details), details["text"]))
    try:
        with api("/api/meetings/retention?days=7", headers={"Origin": base}, method="PUT") as response:
            assert json.load(response)["days"] == 7
        for suffix in ("", "/audio", "/personal-notes", "/export?kind=md"):
            try:
                with api(f"/api/sessions/{identifier}{suffix}"):
                    raise AssertionError("An expired meeting is still readable")
            except HTTPError as error:
                assert error.code == 404
        assert all(row["id"] != identifier for row in json.loads(helper("jardctl", "meetings", "list", "--json")))
        for _ in range(50):
            if retained.is_dir() and not audio.exists() and trashed_export.is_file():
                break
            time.sleep(0.2)
        assert (retained / "Audio" / "source.wav").read_bytes() == b"disposable recording"
        assert "Disposable transcript." in (retained / "Transcript.md").read_text()
        assert (retained / "Notes.md").is_file()
        assert trashed_export.read_bytes() == transcript_contents and not transcript_file.exists()
        assert notes_file.read_bytes() == edited_notes and manual_copy.read_bytes() == transcript_contents
        print("Packaged expiry hid the meeting from all readers and moved its audio, transcript, and notes to macOS Trash.", flush=True)
        print("Packaged expiry moved the renamed managed transcript to Trash and preserved edited notes and manual Markdown.", flush=True)
    finally:
        for entry in (retained, managed_archive, trashed_export):
            if entry.exists():
                os.rename(entry, library / ("Restored disposable " + entry.name))


def verify_manual_meeting_trash(library, helper, api, base, status):
    trash = disposable_trash_location(library, helper)
    identifier = str(uuid4())
    source = library / "audio" / identifier / "source.wav"
    source.parent.mkdir()
    source.write_bytes(b"Disposable meeting audio.")
    archive = trash / f"Jard Meeting {identifier}"
    text = "Disposable meeting transcript."
    transcript = {"text": text, "durationSeconds": 1, "words": [],
                  "segments": [{"speaker": 1, "start": 0.1, "end": 0.8, "text": text}], "speakerNames": {"1": "You"}}
    with closing(sqlite3.connect(library / "sessions.sqlite3", timeout=10)) as db, db:
        db.execute("""INSERT INTO sessions(id,name,created,filename,mime,options,duration,processed,status,transcript,search_text)
                   VALUES(?, 'Disposable manual Trash meeting',strftime('%Y-%m-%dT%H:%M:%SZ','now'),'source.wav','audio/wav',
                          '{"meeting":true,"recording":true,"diarization":false}',1,1,?,?,?)""",
                   (identifier, status, json.dumps(transcript), text))
    try:
        with api(f"/api/native/meetings/{identifier}/recording-files",
                 headers={"X-Jard-Token": (library / ".native-token").read_text().strip()}) as response:
            files = json.load(response)
            assert Path(files["folder"]) == source.parent.resolve()
            assert Path(files["path"]) == source.resolve()
        with api(f"/api/sessions/{identifier}/transcript-clipboard") as response:
            assert response.read().decode() == f"**00:00 · You**  \n{text}"
        with api(f"/api/sessions/{identifier}", headers={"Origin": base}, method="DELETE") as response:
            assert response.status == 204
        assert (archive / "Audio" / source.name).read_bytes() == b"Disposable meeting audio."
        assert transcript["text"] in (archive / "Transcript.md").read_text()
        assert (archive / "Meeting.json").is_file() and (archive / "Notes.md").is_file()
        assert not source.exists()
        assert all(row["id"] != identifier for row in json.loads(helper("jardctl", "meetings", "list", "--json")))
        print(f"Packaged {status} meeting passed reveal, copy, and manual removal to macOS Trash.", flush=True)
    finally:
        if archive.exists():
            os.rename(archive, library / ("Restored disposable " + archive.name))


def verify_audio_retention(library, helper, api, base):
    trash = disposable_trash_location(library, helper)
    identifier = str(uuid4())
    folder = library / "audio" / identifier
    folder.mkdir()
    files = {"source.wav": b"Disposable mix.", "mic.wav": b"Disposable mic.", "system.wav": b"Disposable remote."}
    for name, data in files.items():
        (folder / name).write_bytes(data)
    moved = trash / f"Jard Meeting Audio {identifier}"
    transcript = {"text": "Disposable transcript.", "durationSeconds": 1, "segments": [
        {"speaker": 2, "start": 0.1, "end": 0.9, "text": "Disposable transcript."}], "words": []}
    with closing(sqlite3.connect(library / "sessions.sqlite3", timeout=10)) as db, db:
        db.execute("""INSERT INTO sessions(id,name,created,filename,mime,options,duration,processed,status,transcript,search_text)
                   VALUES(?, 'Disposable audio expiry',strftime('%Y-%m-%dT%H:%M:%SZ','now'),'source.wav','audio/wav',
                         json_set(?,'$.meetingEndedAt',strftime('%Y-%m-%dT%H:%M:%SZ','now','-3 days')),1,1,'complete',?,?)""",
                   (identifier, json.dumps({"recording": True, "diarization": False, "meeting": True}),
                    json.dumps(transcript), transcript["text"]))
        notes = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Keep this note."}]}]}
        db.execute("INSERT INTO personal_notes(id,document,revision) VALUES(?,?,1)", (identifier, json.dumps(notes)))
    try:
        with api("/api/meetings/audio-retention?days=1", headers={"Origin": base}, method="PUT") as response:
            assert json.load(response)["days"] == 1
        for _ in range(60):
            if moved.is_dir() and not folder.exists():
                break
            time.sleep(0.2)
        assert {item.name: item.read_bytes() for item in moved.iterdir()} == files
        with api(f"/api/sessions/{identifier}") as response:
            after = json.load(response)
        assert after["audioUrl"] is None and after["audioRemovedAt"]
        assert after["transcript"] == transcript and after["personalNotes"]["revision"] == 1
        with api(f"/api/sessions/{identifier}/export?kind=notes") as response:
            assert b"Keep this note." in response.read()
        print("Packaged audio-only expiry moved the mix and source tracks to macOS Trash while preserving transcripts and notes.", flush=True)
    finally:
        if moved.is_dir():
            os.rename(moved, library / ("Restored disposable audio " + identifier))


def verify_annotation_retention(library, helper, api, base):
    mounted = os.environ.get("JARD_TEST_ANNOTATION_STORAGE")
    destination = Path(mounted).resolve() / ("Disposable retention " + str(uuid4())) if mounted else None
    storage_id = None
    if destination is not None:
        destination.mkdir(mode=0o700)
        storage_endpoint = "/api/native/annotation-storage"
        headers = {"X-Jard-Token": (library / ".native-token").read_text().strip(), "Content-Type": "application/json"}
        body = json.dumps({"folder": str(destination)}).encode()
        with api(storage_endpoint, headers=headers, data=body, method="PUT") as response:
            configured = json.load(response)
        assert configured["custom"] and configured["available"] and Path(configured["folder"]) == destination
        storage_id = configured["storageId"]
    trash = disposable_trash_location(destination or library, helper)
    expired_video, expired_record, older = (str(uuid4()) for _ in range(3))
    video = trash / f"Jaro Annotation Video {expired_video}.mp4"
    archive = trash / f"Jaro Annotation {expired_record}"
    endpoint = "/api/annotations/retention"
    with api(endpoint) as response:
        setting = json.load(response)
    assert setting == {"mediaDays": 7, "recordDays": 30, "includeExisting": False}
    with closing(sqlite3.connect(library / "sessions.sqlite3", timeout=10)) as db, db:
        for key, age, eligible in ((expired_video, 9, 1), (expired_record, 35, 1), (older, 100, 0)):
            folder = destination / f"Jaro Annotation {key}" if destination and eligible else library / "audio" / key
            folder.mkdir(mode=0o700)
            if destination and eligible:
                marker = folder / ".jaro-annotation-id"
                marker.write_text(key)
                marker.chmod(0o600)
            (folder / "source.mp4").write_bytes(b"Disposable video.")
            (folder / "annotation.json").write_text("{}")
            (folder / "contact-sheet.png").write_bytes(b"Disposable contact sheet.")
            options = {"recording": False, "diarization": False, "annotation": {"version": 1, "strokeCount": 1,
                       "source": "display", "frames": [0.5], "manifestUrl": f"/api/sessions/{key}/annotation/manifest",
                       "contactSheetUrl": f"/api/sessions/{key}/annotation/contact-sheet"}}
            if eligible:
                options["annotationRetentionEligible"] = 1
                if storage_id:
                    options["annotationLocation"] = storage_id
            transcript = {"text": "Disposable narration.", "segments": [], "words": []}
            db.execute("""INSERT INTO sessions(id,name,created,filename,mime,options,duration,processed,status,transcript,search_text)
                VALUES(?, 'Disposable annotation',strftime('%Y-%m-%dT%H:%M:%SZ','now',?),'source.mp4','video/mp4',?,
                       1,1,'complete',?,?)""", (key, f"-{age} days", json.dumps(options), json.dumps(transcript), transcript["text"]))
            notes = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Disposable note."}]}]}
            db.execute("INSERT INTO personal_notes(id,document,revision) VALUES(?,?,1)", (key, json.dumps(notes)))
    try:
        headers = {"Origin": base, "Content-Type": "application/json"}
        with api(endpoint, headers=headers, data=json.dumps(setting).encode(), method="PUT") as response:
            assert json.load(response) == setting
        expired_folder = destination / f"Jaro Annotation {expired_record}" if destination else library / "audio" / expired_record
        for _ in range(80):
            if video.is_file() and archive.is_dir() and not expired_folder.exists():
                break
            time.sleep(.2)
        assert video.read_bytes() == b"Disposable video."
        assert (archive / "Capture" / "source.mp4").read_bytes() == b"Disposable video."
        assert "Disposable narration." in (archive / "Narration.md").read_text()
        assert "Disposable note." in (archive / "Notes.md").read_text()
        assert (library / "audio" / older / "source.mp4").is_file()
        with api(f"/api/sessions/{expired_video}") as response:
            saved = json.load(response)
        assert saved["audioUrl"] is None and saved["transcript"]["text"] == "Disposable narration."
        with api(f"/api/sessions/{expired_video}/annotation/contact-sheet") as response:
            assert response.read() == b"Disposable contact sheet."
        paths = json.loads(helper("jardctl", "annotate", "show", expired_video))
        assert paths["videoPath"] is None and Path(paths["manifestPath"]).is_file()
        remaining = {row["id"] for row in json.loads(helper("jardctl", "annotate", "list"))["annotations"]}
        assert {expired_video, older} <= remaining and expired_record not in remaining
        volume = " from a separate APFS volume" if destination else ""
        print(f"Packaged annotation expiry moved video and full records{volume} to macOS Trash and kept legacy captures.", flush=True)
    finally:
        for entry in (video, archive):
            if entry.exists():
                os.rename(entry, (destination or library) / ("Restored disposable " + entry.name))


def run(app, model_source):
    app = app.resolve()
    helpers = app / "Contents/Helpers"
    subprocess.run(["codesign", "--verify", "--deep", "--strict", app], check=True)
    check_linked_libraries(app)
    with (app / "Contents/Info.plist").open("rb") as f:
        info = plistlib.load(f)
        assert info["LSMinimumSystemVersion"] == "26.0" and info["NSCalendarsFullAccessUsageDescription"]
        assert info["NSCameraUsageDescription"]
    with tempfile.TemporaryDirectory(prefix="jard package ") as scratch:
        work = Path(scratch)
        library = work / "library"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        env = dict(HOME=str(work), PATH="/usr/bin:/bin", RUMBLE_DATA_DIR=str(library),
                   RUMBLE_LOG_DIR=str(work / "logs"), RUMBLE_PORT=str(port))
        def helper(*args):
            return subprocess.check_output([str(helpers / args[0]), *args[1:]], cwd=work, env=env, text=True)
        preferences = work / "Library" / "Preferences" / (info["CFBundleIdentifier"] + ".plist")
        preferences.parent.mkdir(parents=True)
        preferences.write_bytes(plistlib.dumps({
            "JardSystemDictation": {"retention": 7},
            "JardSystemDictationHistory": [
                {"id": "stored", "time": time.time() - 60, "app": "Notes", "text": "Ready.",
                 "raw": "The paperback shipment is ready.", "kind": "dictation", "mode": "app:Notes"},
                {"id": "expired", "time": time.time() - 8 * 86_400, "text": "Paperback archive."},
                *[{"id": f"retained-{index}", "time": time.time() - 100 - index, "text": "Retained entry."}
                  for index in range(280)],
            ],
        }, fmt=plistlib.FMT_BINARY))
        preferences.chmod(0o600)
        before = preferences.read_bytes(), preferences.stat().st_mtime_ns
        files_before = set(work.rglob("*"))
        history = json.loads(helper("jardctl", "history", "--search", "paperbak", "--json", "--raw"))
        assert history["version"] == 1 and [entry["id"] for entry in history["entries"]] == ["stored"], history
        assert history["entries"][0]["mode"] == "app:Notes" and "shipment" in history["entries"][0]["raw_text"]
        deep_page = json.loads(helper("jardctl", "history", "--grep", "Retained entry", "--offset", "270", "--limit", "5", "--json"))
        assert [entry["id"] for entry in deep_page["entries"]] == [f"retained-{i}" for i in range(270, 275)]
        assert deep_page["truncated"]
        assert json.loads(helper("jardctl", "annotate", "list")) == {"version": 1, "annotations": []}
        assert not (library / ".native-token").exists() and not (library / ".native-control").exists()
        assert before == (preferences.read_bytes(), preferences.stat().st_mtime_ns)
        assert files_before == set(work.rglob("*"))
        print("Packaged CLI read and ranked retained dictations without a running host or engine.", flush=True)
        log_dir = work / "logs"
        log_dir.mkdir(mode=0o700)
        (log_dir / "live.ndjson").write_text('{"kind":"dictation","phase":"saved"}\n', encoding="utf8")
        (log_dir / "server.log").write_text("Engine preparing.\nEngine ready.\n", encoding="utf8")
        assert json.loads(helper("jardctl", "logs", "-n", "1"))["phase"] == "saved"
        assert helper("jardctl", "logs", "--process", "-n", "1") == "Engine ready.\n"
        assert not (library / ".native-control").exists()
        print("Packaged CLI read both local logs with the app closed.", flush=True)
        silent = work / "silent.wav"
        with wave.open(str(silent), "wb") as audio:
            audio.setparams((1, 2, 16_000, 0, "NONE", ""))
            audio.writeframes(bytes(16_000))
        silent_caf = silent.with_suffix(".caf")
        subprocess.run(["/usr/bin/afconvert", "-f", "caff", "-d", "LEI16", silent, silent_caf], check=True)
        for clip in (silent, silent_caf):
            result = subprocess.run([str(helpers / "jardctl"), "transcribe", clip.name, "--json"],
                                    cwd=work, env=env, capture_output=True, timeout=20)
            assert result.returncode == 4 and not result.stdout, result
            assert json.loads(result.stderr)["code"] == "no-speech"
        bad = work / "unsupported.txt"
        bad.write_text("Fixture without audio.")
        result = subprocess.run([str(helpers / "jardctl"), "transcribe", bad.name, "--json"],
                                cwd=work, env=env, capture_output=True, timeout=20)
        assert result.returncode == 1 and not result.stdout and "could not be opened" in json.loads(result.stderr)["error"]
        print("Packaged CLI decoded relative WAV and CAF files, and rejected unsupported input before loading the model.", flush=True)
        pinned = json.loads(helper("jardctl", "model", "status", "--json"))
        full = json.loads(helper("jard-engine", "model-status"))
        assert not pinned["ready"] and not full["ready"] and 0 < pinned["bytes"] < full["bytes"]
        audible = work / "audible.wav"
        with wave.open(str(audible), "wb") as audio:
            audio.setparams((1, 2, 16_000, 0, "NONE", ""))
            audio.writeframes(b"\x00\x00\x10\x00" * 8_000)
        result = subprocess.run([str(helpers / "jardctl"), "transcribe", audible.name, "--json"],
                                cwd=work, env=env, capture_output=True, timeout=20)
        assert result.returncode == 1 and not result.stdout, result
        assert json.loads(result.stderr)["code"] == "model-unavailable"
        assert not (library / "models").exists()
        print("Packaged CLI reported the missing speech model without downloading in JSON mode.", flush=True)
        assert json.loads(helper("jardctl", "--json", "status"))["engine"] == "stopped"
        print("Clean model setup and default microphone:", helper("jard-capture", "devices").strip(), flush=True)
        for name in ("ffmpeg", "ffprobe"):
            assert "7.1.1" in helper(name, "-version")
        filters = {line.split()[1] for line in helper("ffmpeg", "-filters").splitlines() if len(line.split()) > 1}
        muxers = {line.split()[1] for line in helper("ffmpeg", "-muxers").splitlines() if len(line.split()) > 1}
        assert {"amix", "apad"} <= filters and "mp4" in muxers
        if model_source is None:
            return
        assert json.loads(helper("jard-engine", "models", "--from", str(model_source.resolve())).splitlines()[-1])["ready"]
        previous_logs = []
        for name, filler, recent in (
            ("live.ndjson", b'{"kind":"fixture","phase":"old"}\n', b'{"phase":"saved"}\n'),
            ("server.log", b"Expired engine message.\n", b"Kept recent engine message.\n"),
        ):
            destination = log_dir / name
            with destination.open("wb") as log:
                log.write(filler * (10 * 1024 * 1024 // len(filler) + 1))
                log.write(recent)
            previous_logs.append((destination, destination.stat().st_ino, recent))
        helper("jard-engine", "start")
        def api(path, data=None, headers=None, *, method=None):
            req = Request(base + path, data=data, headers=headers or {}, method=method)
            return urlopen(req, timeout=120)
        with api("/api/status") as r:
            status = json.load(r)
        expected = hashlib.sha256(os.fsencode(library.resolve())).hexdigest()
        assert status["libraryId"] == expected, "The engine is serving a different library."
        try:
            status = wait_ready(api)
            assert status["status"] == "ready" and status["diarization"], status["status"]
            for log, inode, recent in previous_logs:
                for _ in range(30):
                    if log.stat().st_size <= 6 * 1024 * 1024:
                        break
                    time.sleep(.1)
                assert log.stat().st_ino == inode and log.stat().st_size <= 6 * 1024 * 1024
                with log.open("rb") as stream:
                    stream.seek(-min(65_536, log.stat().st_size), os.SEEK_END)
                    tail = stream.read()
                assert recent in tail
                if log.name == "server.log":
                    assert b"Started server process" in tail
            print("Packaged engine startup compacted both diagnostic logs in place and continued writing.", flush=True)
            with api("/") as r:
                assert b"<html" in r.read()
            audio = work / "sample.aiff"
            subprocess.run(["/usr/bin/say", "-v", "Samantha", "-o", audio,
                            "The books for the library arrive on Monday."], check=True, timeout=20)
            sdk_file = app / "Contents/Resources/sdk/typescript/index.mjs"
            sdk_record = library / ".jard-sdk.json"
            assert sdk_file.is_file() and sdk_record.stat().st_mode & 0o777 == 0o600
            if node := shutil.which("node"):
                subprocess.run([node, "--input-type=module", "-", str(sdk_file), str(audio), str(library)], check=True,
                               text=True, timeout=50, input=r"""
import assert from "node:assert/strict"
import { readFile } from "node:fs/promises"
import { pathToFileURL } from "node:url"
const [modulePath, file, library] = process.argv.slice(2)
const { connect } = await import(pathToFileURL(modulePath).href)
const client = await connect({ library })
assert.equal(await client.health().then(health => health.status), "ready")
assert.equal((await client.capabilities()).serviceCapture, false)
assert.equal((await client.models.list())[0].id, "Reson8/Rumble-2-Flash")
const result = await client.transcribe(await readFile(file), { languages: ["en"] })
assert.match(result.transcript, /library/i)
assert.ok(result.durationMs > 1000 && result.durationMs < 15000)
console.log("Packaged Node SDK verified endpoint ownership and transcribed real audio with Rumble2 Flash.")
""")
                with api("/api/sessions") as response:
                    assert not json.load(response), "SDK transcription must not save a Session."
                assert not list(library.glob(".upload-*"))
            with api("/api/transcribe-file?name=sample.aiff&language=en&diarization=true", audio.read_bytes(),
                     {"Content-Type": "application/octet-stream"}) as r:
                assert r.headers["X-Session-ID"]
                key = r.headers["X-Session-ID"]
                events = [json.loads(line) for line in r if line.strip()]
            assert events[-1].get("done"), events[-1]
            with api("/api/sessions/" + key) as r:
                session = json.load(r)
            assert session["status"] == "complete" and "library" in session["transcript"]["text"].lower()
            assert session["transcript"]["words"] and session["transcript"]["segments"]
            with api("/api/sessions/" + key + "/audio") as r:
                assert r.read() == audio.read_bytes()
            print("Packaged speech, word alignment, speakers, web assets, shared session and audio playback passed.", flush=True)
            token = {"X-Jard-Token": (library / ".native-token").read_text().strip()}
            export_folder = (work / "Meeting Transcripts").resolve()
            export_folder.mkdir()
            export_settings = "/api/native/meeting-exports"
            export_headers = {**token, "Content-Type": "application/json"}
            source_text = "Point one, order 1200 labels. Point two, keep the 12.5% discount."
            for style, markers in (
                ("Write a clear task list and keep the wording of each task.", ("1. ", "2. ")),
                ("Use a hyphen bullet for each task and keep the wording. Do not number the tasks.", ("- ", "- ")),
            ):
                request = json.dumps({"text": source_text, "instruction": style}).encode()
                with api("/api/native/dictation-rewrite", request, export_headers) as response:
                    lines = [line.strip() for line in json.load(response)["text"].splitlines() if line.strip()]
                assert len(lines) == 2 and all(line.startswith(marker) for line, marker in zip(lines, markers)), lines
                assert "1200" in lines[0] and "12.5%" in lines[1], lines
            print("Packaged local Qwen formatted spoken tasks with numbered and custom markers and preserved numeric literals.", flush=True)
            with api(export_settings, json.dumps({"folder": str(export_folder)}).encode(), export_headers, method="PUT") as r:
                assert json.load(r)["folder"] == str(export_folder)
            with api(f"{export_settings}/notes", b'{"enabled":true}', export_headers, method="PUT") as r:
                assert json.load(r)["separateNotes"] is True
            with api("/api/native/staging?recording=true&meeting=true", b"", token) as r:
                draft = json.load(r)
            mixed = Path(draft["path"])
            helper("ffmpeg", "-v", "error", "-y", "-i", str(audio), "-ar", "48000", "-ac", "1",
                   "-fflags", "+bitexact", "-f", "wav", str(mixed))
            with wave.open(str(mixed)) as wav:
                samples = array("h", wav.readframes(wav.getnframes()))
            second_voice = work / "Second voice.aiff"
            subprocess.run(["/usr/bin/say", "-v", "Daniel", "-o", second_voice,
                            "Rachel will bring the presentation after lunch. I will reserve the room before everyone arrives."],
                           check=True, timeout=20)
            second_wav = second_voice.with_suffix(".wav")
            helper("ffmpeg", "-v", "error", "-i", str(second_voice), "-ar", "48000", "-ac", "1", str(second_wav))
            samples.extend(array("h", [0]) * 24_000)
            with wave.open(str(second_wav)) as wav:
                samples.extend(array("h", wav.readframes(wav.getnframes())))
            samples.extend(array("h", [0]) * 24_000)
            silence = array("h", [0]) * len(samples)
            mix = array("h", (round(sample / 2) for sample in samples))
            for path, track in ((mixed, mix), (mixed.with_suffix(".system.wav"), silence), (mixed.with_suffix(".mic.wav"), samples)):
                with wave.open(str(path), "wb") as wav:
                    wav.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
                    wav.writeframes(track.tobytes())
            microphone_wav = mixed.with_suffix(".mic.wav").read_bytes()
            system_wav = mixed.with_suffix(".system.wav").read_bytes()
            stage = f"/api/native/staging/{draft['id']}"
            journal = mixed.with_suffix(".rolling.ndjson")
            title_text = "ועדת השיווק החליטה להשיק את המוצר ביום שלישי."
            title_chunk = {"text": title_text, "language": "English", "meetingSpeakerMode": "mixed",
                           "segments": [{"start": 0, "end": 3, "text": title_text, "speaker": 1}],
                           "words": [], "activity": []}
            journal.write_text(json.dumps([0, 144_000, title_chunk], ensure_ascii=False) + "\n", encoding="utf-8")
            with mixed.with_suffix(".recording").open("rb") as marker:
                fcntl.flock(marker, fcntl.LOCK_EX)
                with api(f"{stage}/meeting-title?through=144000", b"", token) as r:
                    title = json.load(r)["title"]
                assert any("א" <= char <= "ת" for char in title), title
            print("Packaged local Qwen titled a live Hebrew meeting in Hebrew despite an English ASR tag.", flush=True)
            with api(f"{stage}/preview?meeting=true&start=0&frames={len(samples)}&language=en", b"", token) as r:
                live = json.load(r)
            assert live["meetingSpeakerMode"] == "mixed" and "library" in live["text"].lower(), live
            assert "rachel" in live["text"].lower() and len({turn["speaker"] for turn in live["segments"]}) >= 2, live
            rolling = (json.dumps([0, len(samples), live]) + "\n").encode()
            journal.write_bytes(rolling)
            with api("/api/meetings/events", headers={"Authorization": "Bearer " + token["X-Jard-Token"]}) as event_stream:
                assert event_stream.headers.get_content_type() == "text/event-stream"
                with api(f"{stage}/submit?recording=true&recover=true&name=Meeting&language=en", b"", token) as r:
                    meeting_key = json.load(r)["id"]
                while line := event_stream.readline():
                    if line.startswith(b"data: "):
                        assert json.loads(line[6:]) == {"type": "meeting.finished", "id": meeting_key}
                        break
                else:
                    raise AssertionError("The packaged engine did not publish meeting.finished")
            for _ in range(120):
                with api(f"/api/sessions/{meeting_key}") as r:
                    meeting = json.load(r)
                if meeting["status"] != "processing":
                    break
                time.sleep(0.5)
            assert meeting["status"] == "complete", meeting
            assert meeting["meetingExport"]["status"] == "ready", meeting["meetingExport"]
            with api(f"{export_settings}/{meeting_key}", headers=token) as r:
                managed = Path(json.load(r)["path"])
            assert managed.parent == export_folder and managed.name == meeting["meetingExport"]["filename"]
            assert managed.name.endswith(".transcript.md"), managed
            assert "library" in managed.read_text().lower()
            with api(export_settings, b'{"folder":null}', export_headers, method="PUT") as r:
                assert json.load(r)["folder"] is None
            lease = f"/api/clients/{uuid4()}"
            try:
                for attempt in range(180):
                    if attempt % 20 == 0:
                        with api(lease, b"", {"Origin": base}) as r:
                            assert r.status == 204
                    with api(f"/api/sessions/{meeting_key}/managed-export") as r:
                        export = json.load(r)
                    with api(f"/api/sessions/{meeting_key}/meeting-brief") as r:
                        brief = json.load(r)
                    if export["notesFilename"] or export["error"] or (brief and brief["status"] == "error"):
                        break
                    time.sleep(0.5)
            finally:
                with api(lease, headers={"Origin": base}, method="DELETE") as r:
                    assert r.status == 204
            if not brief or brief["status"] != "ready":
                log = (log_dir / "server.log").read_text()
                raise AssertionError((brief, [line for line in log.splitlines() if "Meeting summary failed" in line]))
            assert export["notesFilename"] == managed.name.replace(".transcript.md", ".notes.md"), export
            notes = (export_folder / export["notesFilename"]).read_text()
            assert brief["summary"] in notes and brief["summary"] in managed.read_text()
            assert "[Open transcript](" in notes
            print("Packaged Qwen updated the selected meeting transcript and published matching linked notes.", flush=True)
            old_notes = export_folder / export["notesFilename"]
            title = f"Signed export {uuid4().hex[:8]}"
            with api(f"{export_settings}/descriptive", b'{"enabled":true}', export_headers, method="PUT") as r:
                assert json.load(r)["descriptiveNames"] is True
            with api(f"/api/sessions/{meeting_key}/meeting-title", json.dumps({"title": title}).encode(),
                     {"Origin": base, "Content-Type": "application/json"}, method="PUT") as r:
                renamed = json.load(r)
            assert renamed["title"] == title and not renamed["export"]["refreshing"], renamed
            renamed_transcript = export_folder / renamed["export"]["filename"]
            renamed_notes = export_folder / renamed["export"]["notesFilename"]
            assert renamed_transcript.name == f"{managed.name[:15]} {title}.transcript.md", renamed_transcript
            assert renamed_notes.name == renamed_transcript.name.replace(".transcript.md", ".notes.md")
            assert not managed.exists() and not old_notes.exists()
            with api(f"{export_settings}/{meeting_key}", headers=token) as r:
                assert Path(json.load(r)["path"]) == renamed_transcript
            assert f"[{title}]" in renamed_notes.read_text() or title in renamed_notes.read_text().splitlines()[0]
            assert renamed_transcript.name.replace(" ", "%20") in renamed_notes.read_text()
            print("Packaged title editing renamed the linked pair and Reveal resolved its new location.", flush=True)
            assert not journal.exists()
            transcript = meeting["transcript"]
            assert transcript["text"].lower().count("library") == 1 and "rachel" in transcript["text"].lower(), transcript
            assert meeting["diarization"] and "speakerNames" not in transcript, meeting
            assert len({turn["speaker"] for turn in transcript["segments"] if turn["speaker"]}) >= 2, transcript
            assert len(transcript["speakerActivity"]) >= 2 and transcript["words"], transcript
            assert meeting["meeting"] and meeting["meetingProvider"] == "Meeting"
            with api(f"/api/native/meetings/{meeting_key}/excerpt", headers=token) as r:
                excerpt = json.load(r)
            assert excerpt["settled"] and excerpt["available"] and excerpt["labels"] == {}
            assert {turn["speaker"] for turn in excerpt["turns"]} == {turn["speaker"] for turn in transcript["segments"]}
            with api(f"/api/sessions/{meeting_key}/export?kind=srt") as r:
                assert b"Speaker " in r.read()
            removed_audio = disposable_trash_location(library, helper) / f"Jard Meeting Audio {meeting_key}"
            try:
                with api(f"/api/sessions/{meeting_key}/audio", headers={"Origin": base}, method="DELETE") as r:
                    assert json.load(r)["audioRemovedAt"]
                assert (removed_audio / "mic.wav").read_bytes() == microphone_wav
                assert (removed_audio / "system.wav").read_bytes() == system_wav
            finally:
                if removed_audio.is_dir():
                    os.rename(removed_audio, work / "Restored disposable meeting audio")
            listed = json.loads(helper("jardctl", "meetings", "list", "--since", "1h", "--json"))
            assert [(row["id"], bool(row["audioRemovedAt"])) for row in listed] == [(meeting_key, True)], listed
            shown = json.loads(helper("jardctl", "meetings", "show", "latest", "--json"))
            assert shown["transcriptSource"] == "final" and shown["turns"][0]["speaker"].startswith("Speaker "), shown
            excerpt = json.loads(helper("jardctl", "meetings", "show", "latest", "--to", "1.5s", "--json"))
            assert excerpt["range"] == {"from_ms": 0, "to_ms": 1500}
            assert excerpt["turns"] == [turn for turn in shown["turns"] if round(turn["start"] * 1000) < 1500]
            companion = helper("jardctl", "meetings", "show", "latest", "--from", "0.5s", "--md")
            assert "- **Range:** 00:00.500–end" in companion and companion.count("## Summary") == 1
            request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "get_transcript",
                                             "arguments": {"meeting_id": meeting_key, "to": 1.5}}}) + "\n"
            answer = json.loads(subprocess.check_output([helpers / "jardctl", "mcp"], input=request,
                                                        cwd=work, env=env, text=True, timeout=10))
            transcript_view = json.loads(answer["result"]["content"][0]["text"])
            assert transcript_view["range"] == excerpt["range"] and transcript_view["turns"] == excerpt["turns"]
            hits = json.loads(helper("jardctl", "meetings", "search", '"the library"', "--json"))
            assert hits[0]["id"] == meeting_key and "**the** **library**" in hits[0]["snippet"].lower(), hits
            calendar = {"title": "Architecture / review", "attendees": ["Riley"]}
            with api("/api/native/staging?recording=true&meeting=true&provider=Teams", json.dumps(calendar).encode(),
                     {**token, "Content-Type": "application/json"}) as r:
                damaged = json.load(r)
            # Only the mixed file survived this take. The stage still records the provider.
            with wave.open(damaged["path"], "wb") as wav:
                wav.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
                wav.writeframes(samples.tobytes())
            Path(damaged["path"]).with_suffix(".rolling.ndjson").write_bytes(rolling + b'[broken')
            draft = json.loads(helper("jardctl", "meetings", "show", damaged["id"], "--json"))
            assert draft["staged"] and draft["transcriptSource"] == "rolling" and draft["turns"][0]["speaker"].startswith("Speaker "), draft
            with api(f"/api/native/staging/{damaged['id']}/submit?recording=true&recover=true&name=Recovered&language=en", b"", token) as r:
                recovered_id = json.load(r)["id"]
            for _ in range(120):
                with api(f"/api/sessions/{recovered_id}") as r:
                    recovered = json.load(r)
                if recovered["status"] != "processing":
                    break
                time.sleep(0.5)
            assert recovered["status"] == "complete" and recovered["meeting"] and recovered["meetingProvider"] == "Teams", recovered
            assert recovered["meetingCalendar"] == calendar and recovered["name"] == calendar["title"]
            assert "recordingTracks" not in recovered and recovered["audioUrl"]
            assert recovered["diarization"] and "speakerNames" not in recovered["transcript"]
            print("Packaged Rumble2 Flash and Nemotron distinguished two speakers on the mic with silent Mac audio. Meetings also passed completion events, search, subtitles, audio removal, and recovery.", flush=True)

            with api("/api/native/staging?recording=true", b"", token) as r:
                mic_stage = json.load(r)
            mic_path = Path(mic_stage["path"])
            for source in ("mic", "system"):
                assert not mic_path.with_suffix(f".{source}.wav").exists()
            mic_path.write_bytes(microphone_wav)
            with api(f"/api/native/staging/{mic_stage['id']}/submit?recording=true&diarization=true&language=en",
                     b"", token) as r:
                mic_id = json.load(r)["id"]
            for _ in range(120):
                with api(f"/api/sessions/{mic_id}") as r:
                    mic_session = json.load(r)
                if mic_session["status"] != "processing":
                    break
                time.sleep(0.5)
            assert mic_session["status"] == "complete" and mic_session["recording"], mic_session
            assert not mic_session.get("meeting") and "recordingTracks" not in mic_session, mic_session
            mic_transcript = mic_session["transcript"]
            assert all(word in mic_transcript["text"].lower() for word in ("library", "rachel")), mic_transcript
            assert mic_session["diarization"] and len(mic_transcript["speakerActivity"]) >= 2, mic_transcript
            with api(mic_session["audioUrl"]) as r:
                assert r.read() == microphone_wav
            print("A regular mic-only recording saved its original WAV and identified two speakers after meeting finalization.", flush=True)

            pending, exhausted, stranded = seed_pending_meetings(library, samples, silence, token, api)
        finally:
            stop_engine(api, base, library)

        try:
            verify_restart_recovery(library, helper, api, pending, exhausted, stranded)
            verify_retention(library, helper, api, base, (meeting_key, renamed_transcript, renamed_notes))
            verify_annotation_retention(library, helper, api, base)
            verify_audio_retention(library, helper, api, base)
            for status in ("complete", "interrupted"):
                verify_manual_meeting_trash(library, helper, api, base, status)
        finally:
            stop_engine(api, base, library)

        verify_embedded_sdk(helpers, sdk_file, audio, library, work, env)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("--models", type=Path, help="Existing pinned model folder to verify and exercise actual inference.")
    args = parser.parse_args()
    run(args.app, args.models)
