"""Exercise the shipping bridge's NDJSON pipe against real local models in a disposable library."""
import asyncio
import base64
import contextlib
from datetime import datetime
import json
import os
from pathlib import Path
import plistlib
import socket
import subprocess
import sys
import tempfile
import time
import wave
from uuid import uuid4

import httpx
import numpy as np

from annotation_agent_fixture import install_agent_fixtures
from opencode_fixture import local_opencode


ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"


async def smoke(root):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {**os.environ, "RUMBLE_PORT":str(port), "RUMBLE_DATA_DIR":str(root/"library"),
           "RUMBLE_LOG_DIR":str(root/"logs"),"JARD_UI_EXECUTABLE":str(root/"jard-ui"), "PYTHONUNBUFFERED":"1",
           "XDG_STATE_HOME":str(root / "agent-state"), "XDG_CONFIG_HOME":str(root / "configuration")}
    transform_helper = os.getenv("JARD_TEST_TRANSFORMS_HELPER")
    if transform_helper:
        (root / "jaro-transforms").symlink_to(Path(transform_helper).resolve())
    env["PATH"] = str(root) + os.pathsep + os.environ.get("PATH", os.defpath)
    agent_targets = install_agent_fixtures(root, env)
    if codec_helpers := os.environ.get("JARD_TEST_CODEC_HELPERS"):
        for name in ("ffmpeg", "ffprobe"):
            (root / name).symlink_to(Path(codec_helpers).resolve() / name)
    base = f"http://127.0.0.1:{port}"
    audio = root/"review.wav"
    external_fixture = Path(os.environ.get("JARD_TEST_ANNOTATION_STORAGE", root)).resolve()
    annotation_storage = external_fixture / ("Disposable Jaro captures " + uuid4().hex)
    annotation_storage.mkdir(mode=0o700)
    subprocess.run(["say","-o",root/"speech.aiff","The library approved twelve history books. The books will arrive on Monday."],check=True)
    subprocess.run(["ffmpeg","-v","error","-i",root/"speech.aiff","-ar","48000","-ac","1","-af","apad=pad_dur=1","-fflags","+bitexact",audio],check=True)
    env["FIXTURE_AUDIO"] = str(audio)
    noise = root / "noise.wav"
    with wave.open(str(noise), "wb") as stream:
        stream.setparams((1, 2, 48_000, 0, "NONE", "not compressed"))
        samples = np.random.default_rng(403).normal(0, 1200, 3 * 48_000).astype("<i2")
        stream.writeframes(samples.tobytes())
    env["FIXTURE_NOISE"] = str(noise)
    local = root / "local.wav"
    subprocess.run(["say", "-v", "Daniel", "-o", root / "local.aiff", "Send a copy to Rachel on Friday."], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-i", root / "local.aiff", "-ar", "48000", "-ac", "1", local], check=True)
    env["FIXTURE_LOCAL_AUDIO"] = str(local)
    with wave.open(str(audio)) as stream:
        remote_samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2")
    late_speech = root / "late-speech.wav"
    # The loud pre-release prefix clears the volume screen. Only the acoustic
    # screen can reject this take when the speech starts in the release tail.
    with wave.open(str(late_speech), "wb") as stream:
        stream.setparams((1, 2, 48_000, 0, "NONE", "not compressed"))
        stream.writeframes(samples.tobytes() + remote_samples[:28_800].tobytes())
    env["FIXTURE_LATE_SPEECH"] = str(late_speech)
    with wave.open(str(local)) as stream:
        local_samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2")
    long_take = root / "long-take.wav"
    last_phrase = 24 * 48_000 + 24_000
    long_samples = np.zeros(last_phrase + len(local_samples) + 24_000, dtype="<i2")
    long_samples[:len(remote_samples)] = remote_samples
    long_samples[14 * 48_000:14 * 48_000 + len(remote_samples)] = remote_samples
    long_samples[last_phrase:last_phrase + len(local_samples)] = local_samples
    with wave.open(str(long_take), "wb") as stream:
        stream.setparams((1, 2, 48_000, 0, "NONE", "not compressed"))
        stream.writeframes(long_samples.tobytes())
    env["FIXTURE_LONG_TAKE"] = str(long_take)
    starts_at = len(remote_samples) + 12_000
    count = starts_at + len(local_samples) + 24_000
    mic_samples = np.zeros(count, dtype="<i2")
    speaker_samples = np.zeros(count, dtype="<i2")
    speaker_samples[:len(remote_samples)] = remote_samples
    mic_samples[3_840:3_840 + len(remote_samples)] = remote_samples * 0.35
    mic_samples[starts_at:starts_at + len(local_samples)] = local_samples
    for key, samples in {"MIC": mic_samples, "SYSTEM": speaker_samples, "MIX": (mic_samples // 2 + speaker_samples // 2)}.items():
        path = root / f"meeting-{key}.wav"
        with wave.open(str(path), "wb") as stream:
            stream.setparams((1, 2, 48_000, 0, "NONE", "not compressed"))
            stream.writeframes(samples.tobytes())
        env[f"MEETING_{key}"] = str(path)
    # Real file panels and audio hardware are interactive. These two helpers supply
    # exactly the paths/events those platform adapters deliver; the rest is production code.
    export_folder = (root / "Managed meeting exports").resolve()
    export_folder.mkdir()
    env["FIXTURE_MEETING_EXPORT"] = str(export_folder)
    dialog=root/"jard-dialog"
    dialog.write_text("#!/usr/bin/env python3\n"+"""import json,os,sys
from pathlib import Path
audio=Path(os.environ["FIXTURE_AUDIO"])
if sys.argv[1]=="meeting-export-folder":
    print(json.dumps({"path":os.environ["FIXTURE_MEETING_EXPORT"]}))
elif sys.argv[1]=="trash":
    item=Path(sys.argv[2])
    trash=audio.parent/"Trash"
    trash.mkdir(exist_ok=True)
    print(json.dumps({"path":str(item.rename(trash/item.name))}))
elif sys.argv[1]=="save" and (audio.parent/"cancel-export").exists():
    print(json.dumps({"path":None}))
else:
    print(json.dumps({"path":str(audio if sys.argv[1]=="open-audio" else audio.parent/("export-"+sys.argv[2]))}))
""")
    capture=root/"jard-capture"
    capture.write_text("#!/usr/bin/env python3\n"+"""import json,shutil,sys,os,time,wave
from threading import Thread
from pathlib import Path

def write_dictation(path):
    if "FixtureSilent" in sys.argv:
        with wave.open(path, "wb") as stream:
            stream.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            stream.writeframes((1000).to_bytes(2, "little", signed=True) * 48_000)
        return
    fixture = next((name for option, name in (("FixtureNoise", "FIXTURE_NOISE"), ("FixtureLocalSpeech", "FIXTURE_LOCAL_AUDIO"),
                   ("FixtureTailOnly", "FIXTURE_LATE_SPEECH"), ("FixtureLongTake", "FIXTURE_LONG_TAKE")) if option in sys.argv),
                   "FIXTURE_AUDIO")
    shutil.copyfile(os.environ[fixture], path)

if sys.argv[1]=="devices":
    print(json.dumps({"microphone":"Fixture microphone"}),flush=True)
elif sys.argv[1]=="warm":
    Path(__file__).with_name("warm-options").write_text(json.dumps(sys.argv[2:]))
    print(json.dumps({"event":"warmReady"}),flush=True)
    report_status = "FixtureWarmStatus" in sys.argv
    if report_status:
        outage = {"event":"warmAvailability","phase":"unavailable","microphone":"","message":"Fixture microphone could not open."}
        for _ in range(2):
            print(json.dumps(outage), flush=True)
        def publish_fallback():
            gate = Path(__file__).with_name("warm-fallback")
            while not gate.exists():
                time.sleep(.02)
            print(json.dumps({"event":"warmAvailability","phase":"fallback","microphone":"Fixture USB","message":""}), flush=True)
        Thread(target=publish_fallback, daemon=True).start()
    elif "FixtureWarmReady" in sys.argv:
        print(json.dumps({"event":"warmAvailability","phase":"ready","microphone":"Fixture saved","message":""}), flush=True)
    stage = None
    meeting_stage = None
    meeting_app = None
    answered_copy = False
    demand = "--on-demand" in sys.argv
    for line in sys.stdin:
        if line.startswith("{"):
            request = json.loads(line)
            if "annotation" in request:
                if request["action"] == "start":
                    shutil.copyfile(os.environ["FIXTURE_AUDIO"],request["path"])
                print(json.dumps({"event":"warmAnnotation","request":request["annotation"]}),flush=True)
                continue
            if "meeting" in request:
                meeting = request["meeting"]
                def meeting_event(event, **fields):
                    print(json.dumps({"event": event, "sharedMeeting": meeting, **fields}), flush=True)

                recovery_id = "79640d31-cbcd-4202-beda-49a3a94cbdcf"
                if "start" in request:
                    meeting_stage = request["start"]
                    meeting_app = request.get("callApplication")
                    answered_copy = False
                    for suffix, fixture in (("", "MIX"), (".mic.wav", "MIC"), (".system.wav", "SYSTEM")):
                        shutil.copyfile(os.environ["MEETING_"+fixture], meeting_stage+suffix)
                    meeting_event("started", microphone="Shared fixture microphone", microphoneAvailable=True)
                    print(json.dumps({"event":"warmAvailability","phase":"ready","microphone":"Shared fixture microphone","message":""}), flush=True)
                    if meeting_app:
                        meeting_event("meetingMicRecoveryRequest", id=recovery_id)
                    if meeting_app == "us.zoom.xos":
                        assert request["inputDevice"] == "Saved microphone" and request["systemAudio"] == "tapAndScreen" and request["hostPID"] > 1
                        meeting_event("meetingSystemAudio", available=False)
                        meeting_event("meetingMicWarning", active=True)
                        meeting_event("sourceChanged", microphone="Shared fixture microphone", microphoneAvailable=False,
                                      message="Reconnecting the microphone")
                    meeting_event("levels", seconds=4.5, mic=.2, system=.3, microphone="Shared fixture microphone", meetingRms=.15)
                    if meeting_app == "us.zoom.xos":
                        meeting_event("meetingMicWarning", active=False)
                        meeting_event("sourceChanged", microphone="Shared fixture microphone", microphoneAvailable=True,
                                      message="Recording from Shared fixture microphone")
                    meeting_event("meetingSystemAudio", available=True, backend="screen")
                elif request["command"].startswith("copy "):
                    if not answered_copy:
                        answered_copy = True
                        frame = (Path(meeting_stage).stat().st_size - 44) // 2 - 1200 if meeting_app else 192000
                        meeting_event("meetingCopy", request=request["command"][5:], frame=frame)
                elif request["command"].startswith("input "):
                    source = json.loads(request["command"][6:]) or "System default"
                    meeting_event("sourceChanged", microphone=source, message="Recording from " + source)
                elif meeting_app and request["command"] in ("recover-mic " + recovery_id, "defer-mic " + recovery_id):
                    verb = request["command"].split()[0]
                    if verb == "recover-mic":
                        meeting_event("meetingMicRecoveryDone", id=recovery_id)
                    Path(os.environ["FIXTURE_AUDIO"]).with_name(meeting_app + ".mic-recovery").write_text(verb)
                elif request["command"]=="stop":
                    assert meeting_stage is not None
                    meeting_event("finished", reason="Stopped")
                    meeting_event("takeEnded")
                    meeting_stage = None
                continue
            if "meter" in request:
                print(json.dumps({"event":"warmMeter","request":request["meter"],"peak":None}),flush=True)
                continue
            stage = request["start"]
            if demand and "FixtureFailedOpen" in sys.argv:
                print(json.dumps({"event":"error", "message":"Fixture microphone failed to open.",
                                  "interruption":"microphone", "failedAt":123.22}),flush=True)
                print(json.dumps({"event":"takeEnded"}),flush=True)
                stage = None
                continue
            if demand and "FixtureAbandonedOpen" in sys.argv:
                continue
            write_dictation(stage)
            name = "Warm fixture" if "queuedAt" not in request else "Warm queued " + str(request["queuedAt"])
            using_fallback = "--input" in sys.argv and sys.argv[sys.argv.index("--input")+1] == "Saved microphone"
            started = {"event":"started", "microphone":name, "usingDefaultMicrophone":using_fallback}
            if demand:
                started.update(microphone="Fixture microphone", openedAt=123.34)
                if "FixtureFallback" in sys.argv:
                    assert "--avoid-bluetooth" in sys.argv and request["preRoll"] is False
                    started.update(microphone="System fixture input", usingDefaultMicrophone=True)
            print(json.dumps(started),flush=True)
            if demand:
                print(json.dumps({"event":"levels","seconds":4.5,"mic":0.2,"system":0.3,
                                  "microphone":started["microphone"]}),flush=True)
            continue
        if stage is None:
            continue
        command = line.split()
        if "FixtureSlowWarm" in sys.argv or "FixtureAbandonedOpen" in sys.argv:
            print(json.dumps({"event": "openingAbandoned"}), flush=True)
        result = {"event":"finished","reason":"Stopped"}
        if "FixtureAbandonedOpen" in sys.argv:
            assert command[0] == "finish" and len(command) == 2
        elif "FixtureTailOnly" in sys.argv:
            assert float(command[1]) > 0
            result["releaseFrame"] = 3 * 48_000
        elif "FixtureInterrupted" in sys.argv:
            result.update(reason="Fixture microphone disconnected.", interruption="microphone",
                          capturedThrough=123.75, failedAt=123.79, hasLiveAudio=True)
        elif len(command) == 2 and command[0] == "finish":
            assert float(command[1]) > 0
            with wave.open(stage, "rb") as stream:
                result["releaseFrame"] = stream.getnframes() - 24_000
        print(json.dumps(result),flush=True)
        print(json.dumps({"event":"takeEnded"}),flush=True)
        stage = None
    if report_status:
        print(json.dumps({"event":"warmAvailability","phase":"unavailable","microphone":"","message":"Retired worker event."}), flush=True)
elif Path(sys.argv[sys.argv.index("--output")+1]).parent.name.startswith("capture-"):
    path = Path(sys.argv[sys.argv.index("--output")+1])
    assert "--meter" in sys.argv and "--input" in sys.argv and "--cold-recovery" not in sys.argv
    uid = sys.argv[sys.argv.index("--input")+1]
    assert uid in ("FixtureMic", "FixtureLost", "FixtureEmpty")
    with wave.open(os.environ["FIXTURE_AUDIO"], "rb") as source, wave.open(str(path), "wb") as destination:
        destination.setparams(source.getparams())
        if uid != "FixtureEmpty":
            destination.writeframes(source.readframes(source.getnframes()))
    print(json.dumps({"event":"started","microphone":uid}),flush=True)
    print(json.dumps({"event":"levels","seconds":4.5,"mic":0.2,"micRms":0.1}),flush=True)
    if uid != "FixtureMic":
        print(json.dumps({"event":"error","message":"Microphone disconnected"}),flush=True)
        sys.exit(1)
    sys.stdin.readline()
    print(json.dumps({"event":"finished","reason":"Stopped"}),flush=True)
elif ("--mode" in sys.argv and sys.argv[sys.argv.index("--mode")+1] in ("mic","system")
      and Path(__file__).with_name("deny-recording-permissions").exists()):
    source = sys.argv[sys.argv.index("--mode")+1]
    print(json.dumps({"event":"error","permission":source,"message":"macOS declined capture"}),flush=True)
    sys.exit(1)
else:
    output = sys.argv[sys.argv.index("--output")+1]
    call_application = sys.argv[sys.argv.index("--call-application") + 1] if "--call-application" in sys.argv else None
    recovery_id = "79640d31-cbcd-4202-beda-49a3a94cbdcf"
    if "FixtureAbandonedOpen" in sys.argv:
        assert "--cold-recovery" in sys.argv and sys.stdin.readline().startswith("finish ")
        print(json.dumps({"event":"openingAbandoned"}),flush=True)
        sys.exit(0)
    elif "FixtureFailedOpen" in sys.argv:
        assert "--cold-recovery" in sys.argv
        print(json.dumps({"event":"error", "message":"Fixture cold microphone failed to open.",
                          "interruption":"microphone", "failedAt":123.22}),flush=True)
        sys.exit(1)
    write_dictation(output)
    if "--separate-tracks" in sys.argv:
        for name in ("mic", "system"):
            sidecar = Path(output + "." + name + ".wav")
            assert sidecar.is_file()
            with open(os.environ["MEETING_" + name.upper()], "rb") as source:
                sidecar.write_bytes(source.read(44 + 4 * 48000 * 2))
        shutil.copyfile(os.environ["MEETING_MIX"], output)
    opened = {"event":"started","microphone":"Fixture microphone","openedAt":123.34}
    if "FixtureFallback" in sys.argv:
        assert "--cold-recovery" in sys.argv and "--avoid-bluetooth" in sys.argv
        opened.update(microphone="System fixture input", usingDefaultMicrophone=True)
    print(json.dumps(opened),flush=True)
    if call_application:
        assert "--separate-tracks" in sys.argv
        print(json.dumps({"event":"meetingMicRecoveryRequest","id":recovery_id}),flush=True)
    if call_application == "us.zoom.xos":
        assert sys.argv[sys.argv.index("--input") + 1] == "Saved microphone"
        index = sys.argv.index("--system-audio")
        assert sys.argv[index+1:index+3] == ["tapAndScreen", "--host-pid"] and int(sys.argv[index+3]) > 1
        print(json.dumps({"event":"meetingSystemAudio","available":False}),flush=True)
        print(json.dumps({"event":"meetingMicWarning","active":True}),flush=True)
    mode = sys.argv[sys.argv.index("--mode") + 1]
    levels = {"event":"levels","seconds":4.5,"mic":0.2,"system":0 if mode == "mic" else 0.3,
              "microphone":"Fixture microphone"}
    if "--separate-tracks" in sys.argv:
        levels["meetingRms"] = 0.15
    print(json.dumps(levels),flush=True)
    if call_application == "us.zoom.xos":
        print(json.dumps({"event":"meetingMicWarning","active":False}),flush=True)
        print(json.dumps({"event":"meetingSystemAudio","available":True,"backend":"screen"}),flush=True)
    answered_copy = False
    for line in sys.stdin:
        command = line.strip()
        if command.startswith("input ") and call_application:
            uid = json.loads(command[6:])
            name = uid or "System default"
            print(json.dumps({"event":"sourceChanged","microphone":name,"message":"Recording from " + name}),flush=True)
            continue
        if call_application and command in ("recover-mic " + recovery_id, "defer-mic " + recovery_id):
            verb = command.split()[0]
            if verb == "recover-mic":
                print(json.dumps({"event":"meetingMicRecoveryDone","id":recovery_id}),flush=True)
            marker = Path(os.environ["FIXTURE_AUDIO"]).with_name(call_application + ".mic-recovery")
            marker.write_text(verb)
            continue
        if command.startswith("copy ") and "--separate-tracks" in sys.argv:
            if answered_copy:
                continue
            answered_copy = True
            for name in ("mic", "system"):
                shutil.copyfile(os.environ["MEETING_" + name.upper()], output + "." + name + ".wav")
            frame = (os.path.getsize(output) - 44) // 2 - 1200
            print(json.dumps({"event":"meetingCopy","request":command[5:],"frame":frame}),flush=True)
        elif command in ("stop", "finish", "cancel") or command.startswith("finish "):
            if "--separate-tracks" in sys.argv and not answered_copy:
                for name in ("mic", "system"):
                    shutil.copyfile(os.environ["MEETING_" + name.upper()], output + "." + name + ".wav")
            break
    result = {"event":"finished","reason":"Stopped"}
    if "FixtureTailOnly" in sys.argv:
        assert float(command.split()[1]) > 0, command
        result["releaseFrame"] = 3 * 48_000
    elif "FixtureInterrupted" in sys.argv:
        result.update(reason="Fixture microphone disconnected.", interruption="microphone",
                      capturedThrough=123.75, failedAt=123.79, hasLiveAudio=True)
    print(json.dumps(result),flush=True)
""")
    for helper in (dialog,capture):
        helper.chmod(0o700)
    subprocess.run([sys.executable, ENGINE / "server.py", "--ensure"], cwd=ROOT, env=env, check=True)
    p=await asyncio.create_subprocess_exec(sys.executable,"desktop/mac_bridge.py",cwd=ROOT,env=env,
                                           stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    counter=0
    seen=[]
    async def event():
        async with asyncio.timeout(120):
            line=await p.stdout.readline()
        assert line, "Bridge closed unexpectedly"
        value=json.loads(line)
        seen.append(value)
        return value
    async def push(command, **args):
        nonlocal counter
        counter+=1
        id=str(counter)
        p.stdin.write((json.dumps(dict(id=id,command="jard."+command,payload=args))+"\n").encode())
        await p.stdin.drain()
        return id

    async def reply(id, expect_error=False):
        while True:
            row = next((value["response"] for value in seen if value.get("response", {}).get("id") == id), None)
            if row:
                if expect_error is None:
                    return row
                assert row["ok"] != expect_error,row
                return row["error"]["message"] if expect_error else row["result"]
            await event()

    async def rpc(command, *, expect_error=False, **args):
        return await reply(await push(command, **args), expect_error)

    async def wait_event(kind):
        while not any(row.get("detail",{}).get("event")==kind for row in seen):
            await event()
        return next(row["detail"] for row in seen if row.get("detail",{}).get("event")==kind)

    async with httpx.AsyncClient(base_url=base,timeout=120,trust_env=False) as web:
        try:
            assert (await rpc("prepare"))["ready"] and {"navigate":"app"} in seen
            await rpc("diagnosticPhase", phase="inserted", text="Private sample sentence", target="Secret destination")
            await rpc("diagnosticPhase", phase="unknown user-provided phase")
            await rpc("diagnosticTiming", inferenceMs=12, outputWaitMs=0, totalMs=24,
                      captureFinalizeMs=True, deliveryMs="Secret destination", other="Private sample sentence")
            events = (root / "logs" / "live.ndjson").read_text().splitlines()
            assert [(row["kind"], row["phase"]) for row in map(json.loads, events)] == [
                ("desktop", "connected"), ("dictation", "inserted"), ("dictation", "timing"),
            ]
            timing = json.loads(events[-1])
            assert {key: value for key, value in timing.items() if key not in ("time", "kind", "phase")} == {
                "inferenceMs": 12, "outputWaitMs": 0, "totalMs": 24,
            }
            assert all("Private sample sentence" not in row and "Secret destination" not in row for row in events)
            assert (root / "logs" / "live.ndjson").stat().st_mode & 0o777 == 0o600
            assert (root / "logs" / "server.log").stat().st_mode & 0o777 == 0o600
            assert (await rpc("devices"))["microphone"]=="Fixture microphone"
            assert await rpc("inputDevices") == {"microphone": "Fixture microphone", "inputs": []}
            async with asyncio.timeout(90):
                while (await web.get("/api/status")).json()["status"]=="loading":
                    await asyncio.sleep(.25)
            async def approve_sdk_capture():
                sequence = 0
                while True:
                    value = await event()
                    request = value.get("nativeRequest", {})
                    if request.get("action") == "sdk-start":
                        sequence += 1
                        input_device = {4: "FixtureLost", 6: "FixtureEmpty", 7: "FixtureLost", 8: "FixtureLost"}.get(sequence, "FixtureMic")
                        decision = {"ok": True, "result": {"inputDevice": input_device,
                                                            "languages": ["en"], "avoidBluetooth": False}}
                        await push("controlReply", id=request["id"], response=decision)

            approval = asyncio.create_task(approve_sdk_capture())
            sdk = None
            try:
                sdk = await asyncio.create_subprocess_exec("node", "--input-type=module", "-", str(root/"library"),
                                                           cwd=ROOT, stdin=asyncio.subprocess.PIPE)
                await asyncio.wait_for(sdk.communicate(input=b"""
import assert from "node:assert/strict"
import { readFile } from "node:fs/promises"
import { setTimeout as delay } from "node:timers/promises"
import { connect } from "./sdk/typescript/index.mjs"
const [library] = process.argv.slice(2)
const client = await connect({ library })
assert.equal((await client.capabilities()).serviceCapture, true)
const token = JSON.parse(await readFile(library + "/.jard-sdk.json", "utf8"))
const endpoint = "http://127.0.0.1:" + token.port + "/api/sdk/v1/dictations"
const auth = { Authorization: "Bearer " + token.token }
const recording = await client.dictation.start({ source: "Fixture SDK" })
assert.equal(recording.sampleRate, 48000)
const levels = await recording.levels.next()
assert.ok(!levels.done && Math.abs(levels.value.rmsDb + 20) < 0.001 && levels.value.peakDb < 0)
const audio = await recording.audio.next()
assert.ok(!audio.done && audio.value instanceof Float32Array && audio.value.length === 4800)
assert.equal(await (await fetch(endpoint + "/" + recording.id + "/finish", { method: "POST", headers: auth })).status, 404)
assert.equal(await (await fetch(endpoint + "/" + recording.id + "/audio", { headers: { ...auth, "X-Jard-Dictation-Token": "0".repeat(64) } })).status, 404)
await assert.rejects(client.dictation.start(), { code: "dictation-busy" })
await delay(10_250)
const result = await recording.finish()
assert.match(result.transcript, /history books/i)
assert.ok(result.durationMs > 1000)
assert.deepEqual(await recording.finish(), result)
assert.equal((await recording.audio.next()).done, true)
const discarded = await client.dictation.start()
await discarded.cancel()
await assert.rejects(discarded.finish(), { code: "dictation-cancelled" })
const interrupted = await client.dictation.start()
for await (const page of interrupted.audio) assert.ok(page.length)
const replacement = await client.dictation.start()
assert.notEqual(replacement.id, interrupted.id)
assert.match((await interrupted.finish()).transcript, /history books/i)
assert.equal((await replacement.levels.next()).done, false)
assert.match((await replacement.finish()).transcript, /history books/i)
const empty = await client.dictation.start()
await assert.rejects(empty.finish(), { code: "microphone-lost" })
const olderLoss = await client.dictation.start()
for await (const page of olderLoss.audio) assert.ok(page.length)
const newerLoss = await client.dictation.start()
for await (const page of newerLoss.audio) assert.ok(page.length)
await assert.rejects(olderLoss.finish(), { code: "microphone-lost" })
await newerLoss.cancel()
await assert.rejects(newerLoss.finish(), { code: "dictation-cancelled" })
const abandoned = await (await fetch(endpoint, { method: "POST", headers: auth })).json()
const owner = { ...auth, "X-Jard-Dictation-Token": abandoned.ownerToken }
assert.equal((await fetch(endpoint + "/" + abandoned.id + "/levels", { headers: owner })).status, 200)
await delay(10_600)
const status = await (await fetch(endpoint + "/" + abandoned.id + "/finish", { method: "POST", headers: owner })).json()
assert.equal(status.phase, "cancelled")
"""), 70)
                assert sdk.returncode == 0
            finally:
                if sdk and sdk.returncode is None:
                    sdk.kill()
                    await sdk.wait()
                approval.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await approval
            pointer = (root/"library"/".native-control").read_text().strip()
            assert not list(Path(pointer).parent.glob("capture-*"))
            assert not (await web.get("/api/sessions")).json()
            print("SDK microphone ownership, leases, levels, PCM, and cancellation passed. A lost recording released the mic, let the next take start, and kept partial Rumble2 Flash output for its owner.", flush=True)
            async def completed(key):
                async with asyncio.timeout(120):
                    while True:
                        data=(await web.get("/api/sessions/"+key)).raise_for_status().json()
                        if data["status"]=="complete": return data
                        assert data["status"]=="processing",data["status"]
                        await asyncio.sleep(.25)
            opts=dict(languages=["en"],diarization=True)
            take = str(uuid4())
            await rpc("record", mode="mic", live=True, languages=["en"], dictation=take.upper(), diarization=True)
            async with asyncio.timeout(45):
                while not any(row.get("detail",{}).get("event") == "preview" and
                              row["detail"].get("dictation") == take for row in seen):
                    await event()
            await rpc("dictation", action="stop", id=take.upper())
            recorded = await wait_event("dictationReady")
            assert recorded["dictation"] == take
            dictated = await rpc("dictation", action="transcribe", id=recorded["id"], languages=["en"])
            assert "history books" in dictated["text"].lower()
            assert dictated["speechRequestMs"] >= dictated["inferenceMs"] > 0
            assert not (await web.get("/api/sessions")).raise_for_status().json()
            assert not list((root/"library"/"staging").glob("*.dictation"))
            cancelled = str(uuid4())
            await rpc("record", mode="mic", live=True, languages=["en"], dictation=cancelled)
            await rpc("dictation", action="cancel", id=cancelled.upper())
            assert not list((root/"library"/"staging").glob("*.dictation"))
            seen.clear()
            global_id = str(uuid4())
            await rpc("record", mode="mic", dictation=global_id, **{"global":True})
            busy_message = await rpc("record", mode="both", expect_error=True)
            assert "already active" in busy_message
            await rpc("dictation", action="stop", id=global_id)
            recorded = await wait_event("dictationReady")
            assert recorded["dictation"] == global_id
            assert not any(row.get("detail", {}).get("event") == "preview" for row in seen)
            final = await rpc("dictation", action="transcribe", id=recorded["id"],
                              languages=["en"], vocabulary=["library", "history books"], cleanupOnFailure=True)
            assert "history books" in final["text"].lower()
            assert not list((root/"library"/"staging").glob("*.dictation"))
            seen.clear()
            global_id = str(uuid4())
            await rpc("record", mode="mic", dictation=global_id, **{"global":True})
            await rpc("dictation", action="stop", id=global_id)
            recorded = await wait_event("dictationReady")
            rejected = await rpc("dictation", action="transcribe", id=recorded["id"],
                                 languages=["invalid"], cleanupOnFailure=True, expect_error=True)
            assert "language from the list" in rejected
            assert not list((root/"library"/"staging").glob("*.dictation"))
            print("System dictation uses the same Metal model, respects recorder ownership and deletes failed takes.", flush=True)
            seen.clear()
            silent_id = str(uuid4())
            await rpc("record", mode="mic", dictation=silent_id, inputDevice="FixtureSilent", **{"global": True})
            await rpc("dictation", action="stop", id=silent_id)
            silent_stage = (await wait_event("dictationReady"))["id"]
            silent = await rpc("dictation", action="transcribe", id=silent_stage, languages=["en"],
                               outputProfile={"rules": "invalid rule"})
            assert silent == {"text": "", "inferenceMs": 0}, silent
            assert not (root/"library"/"staging"/silent_stage).exists()
            print("Near-silent native dictation skipped transcription and output processing and released its stage.", flush=True)
            seen.clear()
            noise_id = str(uuid4())
            await rpc("record", mode="mic", dictation=noise_id, inputDevice="FixtureNoise", **{"global": True})
            await rpc("dictation", action="stop", id=noise_id)
            noise_stage = (await wait_event("dictationReady"))["id"]
            noise_result = await rpc("dictation", action="transcribe", id=noise_stage, languages=["en"])
            assert noise_result["text"] == "" and noise_result["inferenceMs"] > 0, noise_result
            assert not (root/"library"/"staging"/noise_stage).exists()
            print("The installed Rumble2 Flash encoder screened a louder, speech-free take and released its stage.", flush=True)
            seen.clear()
            tail_id = str(uuid4())
            await rpc("record", mode="mic", dictation=tail_id, inputDevice="FixtureTailOnly", **{"global": True})
            await rpc("dictation", action="stop", id=tail_id, releaseTail=True, releaseAt=100.123)
            tail_stage = (await wait_event("dictationReady"))["id"]
            tail_result = await rpc("dictation", action="transcribe", id=tail_stage, languages=["en"])
            assert tail_result["text"] == "" and tail_result["inferenceMs"] > 0, tail_result
            assert not (root/"library"/"staging"/tail_stage).exists()
            print("Rumble2 Flash screened the pre-release prefix even when the retained audio contained later speech.", flush=True)
            seen.clear()
            long_id = str(uuid4())
            await rpc("record", mode="mic", dictation=long_id, inputDevice="FixtureLongTake", **{"global": True})
            await rpc("dictation", action="stop", id=long_id)
            long_stage = (await wait_event("dictationReady"))["id"]
            long_result = await rpc("dictation", action="transcribe", id=long_stage, languages=["en"])
            long_text = long_result["text"].lower()
            assert "history books" in long_text and "rachel" in long_text, long_result
            assert long_text.index("history books") < long_text.index("rachel"), long_result
            assert not (root/"library"/"staging"/long_stage).exists()
            print("Rumble2 Flash retained the speech before and after a pause-aware dictation boundary.", flush=True)
            seen.clear()
            abandoned = str(uuid4())
            await rpc("record", dictation=abandoned, inputDevice="FixtureAbandonedOpen", **{"global": True})
            await rpc("dictation", action="stop", id=abandoned, releaseTail=True, releaseAt=123.25)
            aborted = await wait_event("error")
            assert aborted["dictation"] == abandoned and aborted["openingAbandoned"] and aborted["noSpeech"], aborted
            assert not list((root / "library" / "staging").glob("*.dictation"))
            seen.clear()
            fallback_id = str(uuid4())
            await rpc("record", dictation=fallback_id, inputDevice="FixtureFallback", avoidBluetooth=True, **{"global": True})
            opened = await wait_event("started")
            assert opened["dictation"] == fallback_id and opened["microphone"] == "System fixture input" and opened["usingDefaultMicrophone"], opened
            await rpc("dictation", action="stop", id=fallback_id)
            await rpc("dictation", action="discard", id=(await wait_event("dictationReady"))["id"])
            seen.clear()
            failed_open = str(uuid4())
            await rpc("record", mode="mic", dictation=failed_open, inputDevice="FixtureFailedOpen", **{"global": True})
            failure = await wait_event("error")
            assert failure["dictation"] == failed_open and failure["interruption"] == "microphone" and failure["failedAt"] == 123.22
            assert not list((root / "library" / "staging").glob("*.dictation"))
            seen.clear()
            lost_id = str(uuid4())
            await rpc("record", mode="mic", dictation=lost_id, inputDevice="FixtureInterrupted", **{"global": True})
            reopened = await wait_event("started")
            assert reopened["dictation"] == lost_id and reopened["openedAt"] == 123.34
            await rpc("dictation", action="stop", id=lost_id)
            lost = await wait_event("dictationReady")
            assert lost["dictation"] == lost_id and lost["global"] and lost["interrupted"], lost
            assert lost["interruption"] == "microphone" and lost["capturedThrough"] == 123.75 and lost["hasLiveAudio"], lost
            assert lost["failedAt"] == 123.79
            recovered = await rpc("dictation", action="transcribe", id=lost["id"], languages=["en"])
            assert "history books" in recovered["text"].lower(), recovered
            assert not (root/"library"/"staging"/lost["id"]).exists()
            print("The authenticated bridge preserved microphone-loss evidence and transcribed the retained prefix.", flush=True)
            assert (await rpc("warm", keep=True, inputDevice="Saved microphone", avoidBluetooth=True))["warm"]
            assert json.loads((root / "warm-options").read_text()) == ["--input", "Saved microphone", "--avoid-bluetooth"]
            seen.clear()
            warm_fallback = str(uuid4())
            await rpc("record", mode="mic", dictation=warm_fallback, inputDevice="Saved microphone",
                      avoidBluetooth=True, keep=True, **{"global": True})
            opened = await wait_event("started")
            assert opened["dictation"] == warm_fallback and opened["microphone"] == "Warm fixture" and opened["usingDefaultMicrophone"]
            await rpc("dictation", action="stop", id=warm_fallback)
            ready_fallback = await wait_event("dictationReady")
            fallback_text = await rpc("dictation", action="transcribe", id=ready_fallback["id"], languages=["en"])
            assert "history books" in fallback_text["text"].lower(), fallback_text
            print("Warm fallback input was reported and produced a Rumble2 Flash transcript.", flush=True)
            assert (await rpc("warm", keep=True, inputDevice="FixtureSlowWarm"))["warm"]
            seen.clear()
            abandoned_warm = str(uuid4())
            await rpc("record", mode="mic", dictation=abandoned_warm, inputDevice="FixtureSlowWarm", keep=True,
                      **{"global": True})
            await wait_event("started")
            await rpc("dictation", action="stop", id=abandoned_warm, releaseTail=True, releaseAt=100.125)
            abandoned_result = await wait_event("error")
            assert abandoned_result["dictation"] == abandoned_warm and abandoned_result["openingAbandoned"]
            assert not list((root / "library" / "staging").glob("*.dictation"))
            print("The bridge discarded staged audio when an earlier warm release abandoned the opening.", flush=True)
            async def warm_notice(request, phase):
                while True:
                    values = [row["detail"] for row in seen if row.get("event") == "jard-microphone"
                              and row.get("detail", {}).get("request") == request]
                    if notice := next((value for value in values if value.get("phase") == phase), None):
                        return notice
                    await event()

            seen.clear()
            outage_request = str(uuid4())
            assert (await rpc("warm", keep=True, inputDevice="FixtureWarmStatus", request=outage_request))["warm"]
            assert (await warm_notice(outage_request, "unavailable"))["message"] == "Fixture microphone could not open."
            (root / "warm-fallback").touch()
            assert (await warm_notice(outage_request, "fallback"))["microphone"] == "Fixture USB"
            fallback_take = str(uuid4())
            await rpc("record", mode="mic", dictation=fallback_take, inputDevice="FixtureWarmStatus", keep=True, **{"global": True})
            assert (await wait_event("started"))["dictation"] == fallback_take
            await rpc("dictation", action="stop", id=fallback_take)
            fallback_stage = await wait_event("dictationReady")
            await rpc("dictation", action="discard", id=fallback_stage["id"])
            ready_request = str(uuid4())
            assert (await rpc("warm", keep=True, inputDevice="FixtureWarmReady", request=ready_request))["warm"]
            assert (await warm_notice(ready_request, "ready"))["microphone"] == "Fixture saved"
            notices = [row["detail"] for row in seen if row.get("event") == "jard-microphone"]
            assert sum(value.get("phase") == "unavailable" for value in notices) == 1
            assert not any(value.get("message") == "Retired worker event." for value in notices)
            print("Warm input availability arrived while idle, kept the take stream intact, and rejected a retired worker event.", flush=True)
            assert (await rpc("warm", keep=True))["warm"]
            legacy = await rpc("output", profiles="default | compact", describe=True)
            assert legacy["profiles"][0]["steps"] == ["compact"] and legacy["profiles"][0]["deadline"] == 60
            profiles = json.dumps([
                {"target": "default", "steps": ["trim"]},
                {"target": "app:Fixture Notes.app", "steps": ["replace", "lowercase", "no-trailing-punctuation"], "vocabulary": ["History Books"],
                 "corrections": "illustrated books => annotated books"},
                {"target": "site:*.example.com", "steps": ["rewrite"], "instruction": "Replace only 'planned' with 'scheduled'.",
                 "vocabulary": ["Rumble2 Flash", "Jard"], "deadline": 35},
            ])
            numeric = await rpc("output", text="sample counts", profiles=json.dumps([{
                "target": "default", "steps": ["rewrite", "sentence"], "corrections": "sample counts => twenty-one point five",
            }]))
            assert numeric["text"] == "Twenty-one point five." and not numeric["warnings"], numeric
            builtin_preview = await rpc("output", text="Hello, WORLD?!…", site="https://xn--bcher-kva.example/other",
                                        profiles="site:https://bücher.example:3000/path | lowercase, no-trailing-punctuation")
            assert builtin_preview["text"] == "hello, world" and not builtin_preview["warnings"], builtin_preview
            quick = await rpc("output", text="JAH ROH AUDIO?!", builtinsOnly=True, aliases=[["jah roh", "Jaro"]], profiles=json.dumps([{
                "target": "default", "steps": ["lowercase", "rewrite", "script", "local:add-label", "no-trailing-punctuation"],
                "corrections": "Jaro Audio => Jaro",
            }]))
            assert quick["text"] == "jaro" and quick["skipped"] == ["rewrite", "script", "local:add-label"] and not quick["warnings"], quick
            if os.getenv("JARD_TEST_OUTPUT_REWRITE"):
                preview = await rpc("output", profiles=profiles, text="We planned 12 books for Jard.",
                                    site="app.example.com", instruction="Never change the word planned.",
                                    vocabulary=["RUMBLE2 FLASH", "shared term"])
                assert preview["profile"] == "site:*.example.com"
                assert preview["vocabulary"] == ["Rumble2 Flash", "Jard", "shared term"]
                assert not preview["warnings"], preview
                assert "scheduled 12 books" in preview["text"].lower(), preview
                print("Scoped instructions and prioritized vocabulary resolved for the local Qwen preview.", flush=True)
                tasks = await rpc("output", text="Point one, order 1200 labels. Point two, keep the 12.5% discount.",
                                  profiles=json.dumps([{"target": "default", "steps": ["rewrite", "lowercase"],
                                                        "instruction": "Write a clear task list and keep the wording of each task."}]))
                assert tasks["text"].splitlines() == ["1. order 1200 labels.", "2. keep the 12.5% discount."], tasks
                assert not tasks["warnings"], tasks
                print("The authenticated output bridge kept numeric literals and ran the next step after a numbered rewrite.", flush=True)
            seen.clear()
            script = root / "output-transform"
            script.write_text("#!/bin/sh\n/bin/cat\n/bin/cat suffix\n")
            script.chmod(0o700)
            suffix = root / "suffix"
            suffix.write_text(" approved output")
            catalog = root / "transforms.json"
            catalog.write_text(json.dumps({"transformations": {
                "add-label": {"name": "Add label", "entry": script.name, "files": [suffix.name]},
            }}))
            review = await rpc("inspectOutputScript", path=str(catalog))
            assert review["transforms"] == [{"id": "add-label", "name": "Add label", "description": ""}], review
            digest = review["digest"]
            await rpc("outputScriptApproval", path=str(catalog), digest=digest)
            with_script = json.dumps([{
                "target": "app:Fixture Notes.app", "steps": ["replace", "lowercase", "no-trailing-punctuation", "local:add-label"],
                "vocabulary": ["History Books"], "corrections": "illustrated books => annotated books",
            }])
            first_global = str(uuid4())
            await rpc("record", dictation=first_global, **{"global":True, "keep":True})
            assert "Finish your recording" in await rpc("desktopQuiesce", expect_error=True)
            assert "Finish your recording" in await rpc("updateQuiesce", expect_error=True)
            await rpc("dictation", action="stop", id=first_global, releaseTail=True)
            first_stage = (await wait_event("dictationReady"))["id"]
            first_work = await push("dictation", action="transcribe", id=first_stage, languages=["en"],
                                    cleanupOnFailure=True, vocabulary=["Shared hint"], outputProfile={"rules": with_script,
                                    "app": "Fixture process", "bundle": "com.apple.Notes", "bundleName": "Fixture Notes",
                                    "script": str(catalog), "scriptDigest": digest,
                                    "aliases": [["history books", "Shelf Catalogue"]],
                                    "replacements": "Shelf Catalogue => illustrated books"})
            second_global = str(uuid4())
            await rpc("record", dictation=second_global, queuedAt=123.88, **{"global":True, "keep":True})
            await rpc("dictation", action="stop", id=second_global, releaseTail=True)
            assert any(row.get("detail", {}).get("microphone") == "Warm queued 123.88" for row in seen)
            stages = [row["detail"] for row in seen if row.get("detail", {}).get("event") == "dictationReady"]
            assert [stage["dictation"] for stage in stages] == [first_global, second_global]
            second_work = await push("dictation", action="transcribe", id=stages[-1]["id"],
                                     languages=["en"], cleanupOnFailure=True, outputProfile={
                                         "rules": '[{"target":"default","steps":["replace","rewrite","sentence"]}]',
                                         "replacements": "history books => archive books"})
            scoped = await reply(first_work)
            assert "annotated books" in scoped["processed"], scoped
            assert scoped["processed"] == scoped["processed"].lower() and scoped["processed"][-1].isalnum(), scoped
            assert scoped["processed"].endswith(" approved output") and not scoped.get("outputWarning"), scoped
            assert scoped["profile"] == "app:Fixture Notes.app"
            assert scoped["processing"]["fallbacks"] == [] and "model" not in scoped["processing"], scoped
            assert scoped["processing"]["latencyMs"] >= 0, scoped
            fallback = await reply(second_work)
            assert "history books" in fallback["text"].lower() and "archive books" in fallback["processed"].lower()
            assert "rewrite instructions" in fallback["outputWarning"]
            assert fallback["processing"]["model"] == "Qwen3.5-2B", fallback
            assert fallback["processing"]["fallbacks"] == ["rewrite"], fallback
            if os.getenv("JARD_TEST_OUTPUT_REWRITE"):
                seen.clear()
                real_id = str(uuid4())
                await rpc("record", dictation=real_id, inputDevice="FixtureLocalSpeech", **{"global": True})
                await rpc("dictation", action="stop", id=real_id)
                real_capture = await wait_event("dictationReady")
                assert real_capture["dictation"] == real_id, real_capture
                real_stage = real_capture["id"]
                rewritten = await rpc("dictation", action="transcribe", id=real_stage, languages=["en"],
                                      outputProfile={"rules": "default | rewrite, lowercase",
                                                     "instruction": "Replace only Friday with Monday."})
                observation = rewritten["processing"]
                assert observation["model"] == "Qwen3.5-2B" and observation["latencyMs"] > 0, rewritten
                assert "friday" in rewritten["text"].lower(), rewritten
                if rewritten.get("outputWarning"):
                    assert observation["fallbacks"] == ["rewrite"], rewritten
                    assert rewritten["processed"] == rewritten["text"].lower(), rewritten
                else:
                    assert not observation["fallbacks"] and "monday" in rewritten["processed"], rewritten
                print("Rumble2 Flash and local Qwen returned processing metadata and preserved the rewrite fallback.", flush=True)
            seen.clear()
            altered_id = str(uuid4())
            suffix.write_text(" changed dependency")
            await rpc("record", dictation=altered_id, **{"global":True, "keep":True})
            await rpc("dictation", action="stop", id=altered_id, releaseTail=True)
            altered_stage = (await wait_event("dictationReady"))["id"]
            altered = await rpc("dictation", action="transcribe", id=altered_stage, languages=["en"],
                                outputProfile={"rules": "default | lowercase, local:add-label", "script": str(catalog), "scriptDigest": digest})
            assert altered["text"] == altered["processed"] and "catalog changed" in altered["outputWarning"], altered
            assert altered["processing"]["fallbacks"] == ["transformations"] and "model" not in altered["processing"], altered
            print("Named transformation and changed dependency fallback passed with Rumble2 Flash.", flush=True)
            if transform_helper:
                assert (await rpc("findOutputConfig"))["name"] == ""
                starter = await rpc("createOutputConfig")
                assert Path(starter["path"]).parent == root.resolve() / "configuration" / "jaro", starter
                assert "jaro.d.ts" in starter["created"], starter
                assert (await rpc("findOutputConfig"))["name"] == "jaro.config.ts"
                assert not (await rpc("createOutputConfig"))["created"]
                starter_review = await rpc("inspectOutputScript", path=starter["path"], fresh=True)
                assert starter_review["transforms"][0]["id"] == "trim-lines", starter_review

                typed_config = root / "jaro.config.ts"
                typed_support = root / "ending.ts"
                typed_support.write_text('export const ending: string = " reviewed output";')
                typed_config.write_text("""
                    import { defineJaroConfig } from "@jaro/commands";
                    import { ending } from "./ending";
                    import { execFile } from "node:child_process";
                    import { promisify } from "node:util";
                    const run = promisify(execFile);
                    export default defineJaroConfig({
                        transformations: { caption: {
                            name: "Caption",
                            async transform(text: string, context: {application: string, browserHost?: string,
                                                                    browserUrl?: string, windowTitle?: string}) {
                                if (context.application !== "Fixture Notes" || context.browserHost !== "output.example") {
                                    throw new Error("Incorrect context");
                                }
                                const detail = context.windowTitle || context.browserUrl;
                                if (detail && (context.windowTitle !== "Fixture window" ||
                                    context.browserUrl !== "https://output.example/doc?q=test#draft")) {
                                    throw new Error("Incorrect page details");
                                }
                                await new Promise(resolve => setTimeout(resolve, 2));
                                const { stdout } = await run("/bin/cat", [], { input: text });
                                console.debug("Fixture transformation finished");
                                return stdout + ending + (detail ? " detailed" : "");
                            }
                        } }
                    });
                    """)
                typed_review = await rpc("inspectOutputScript", path=str(typed_config))
                assert typed_review["kind"] == "embedded" and typed_review["transforms"][0]["id"] == "caption", typed_review
                typed_digest = typed_review["digest"]
                assert typed_review["permissions"]["programs"] and not typed_review["permissions"]["network"], typed_review
                for trial in ("approved", "detailed", "context-revoked", "revoked", "edited"):
                    grants = {"programs": trial != "revoked", "pageContext": trial == "detailed"}
                    await rpc("outputScriptApproval", path=str(typed_config), digest=typed_digest, grants=grants)
                    if trial == "edited":
                        typed_support.write_text('export const ending: string = " newer output";')
                    seen.clear()
                    typed_id = str(uuid4())
                    await rpc("record", dictation=typed_id, **{"global": True, "keep": True})
                    await rpc("dictation", action="stop", id=typed_id, releaseTail=True)
                    typed_stage = (await wait_event("dictationReady"))["id"]
                    typed_take = await rpc("dictation", action="transcribe", id=typed_stage, languages=["en"],
                                           outputProfile={"rules": "default | lowercase, local:caption",
                                                          "script": str(typed_config), "scriptDigest": typed_digest,
                                                          "scriptGrants": grants, "app": "Fixture Notes",
                                                          "site": "output.example", "windowTitle": "Fixture window",
                                                          "browserUrl": "https://output.example/doc?q=test#draft"})
                    if trial == "edited":
                        assert typed_take["processed"] == typed_take["text"] and "configuration changed" in typed_take["outputWarning"], typed_take
                    elif trial == "revoked":
                        assert typed_take["processed"] == typed_take["text"] and "Allow programs" in typed_take["outputWarning"], typed_take
                    else:
                        suffix = " reviewed output" + (" detailed" if trial == "detailed" else "")
                        assert typed_take["processed"] == typed_take["text"].lower() + suffix, typed_take
                print("A granted TypeScript program used page details only with current approval and preserved text on program revocation or changed imports.", flush=True)
                transform_log = [json.loads(line) for line in (root / "logs" / "live.ndjson").read_text().splitlines()]
                assert any(row.get("phase") == "console" and row.get("message") == "Fixture transformation finished"
                           and row.get("level") == "debug" for row in transform_log)
                assert any(row.get("phase") == "program" and row.get("program") == "cat"
                           and row.get("exitCode") == 0 for row in transform_log)

                newer = await rpc("inspectOutputScript", path=str(typed_config))
                assert newer["digest"] != typed_digest and newer["transforms"][0]["name"] == "Caption", newer
                typed_support.write_text("export const ending: string = ;")
                invalid = await rpc("inspectOutputScript", path=str(typed_config))
                assert invalid["stale"] and "ending.ts" in invalid["message"] and not invalid.get("digest"), invalid
                assert invalid["transforms"] == newer["transforms"]
                typed_support.write_text('export const ending: string = " restored output";')
                assert not (await rpc("inspectOutputScript", path=str(typed_config), fresh=True)).get("stale")
                print("The authenticated bridge discovered the starter workspace and refreshed broken and repaired imports.", flush=True)
            assert not (await rpc("warm", keep=False))["warm"]

            tool = await asyncio.create_subprocess_exec(sys.executable, ENGINE / "jardctl.py", "--json", "status",
                        cwd=ROOT, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            while not any("nativeRequest" in row for row in seen):
                await event()
            request = next(row["nativeRequest"] for row in seen if "nativeRequest" in row)
            assert request["action"] == "status"
            await rpc("controlReply", id=request["id"], response={"ok":True,"result":{"phase":"idle","listener":True}})
            out, err=await tool.communicate()
            assert tool.returncode == 0, err
            assert json.loads(out)["desktop"]["listener"]
            async def desktop_command(arguments, action, options, response, *, json_output=True, exit_code=0, expected=None,
                                      environment=None):
                seen.clear()
                tool = await asyncio.create_subprocess_exec(sys.executable, ENGINE / "jardctl.py", *(("--json",) if json_output else ()), *arguments,
                            cwd=ROOT, env=environment or env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                while not any("nativeRequest" in row for row in seen):
                    await event()
                request = next(row["nativeRequest"] for row in seen if "nativeRequest" in row)
                if callable(options):
                    options = options(request)
                assert request == {"id": request["id"], "action": action, **options}
                await rpc("controlReply", id=request["id"], response={"ok": True, "result": response})
                out, err = await tool.communicate()
                assert tool.returncode == exit_code, (out, err)
                if exit_code == 4:
                    assert not out and json.loads(err)["code"] == "no-speech", (out, err)
                elif json_output:
                    assert json.loads(out) == (response if expected is None else expected), (out, err)
                else:
                    assert out.decode() == response["text"] + "\n", (out, err)

            library = root / "library"
            for credentials, confirm, message in (
                ("not-the-library-token", True, "Invalid Jard control credentials."),
                ((library / ".native-token").read_text().strip(), 1, "Confirm deletion of the entire dictation history."),
            ):
                socket_path = (library / ".native-control").read_text().strip()
                reader, writer = await asyncio.open_unix_connection(socket_path)
                writer.write(json.dumps({"token": credentials, "action": "history-clear", "confirm": confirm}).encode() + b"\n")
                await writer.drain()
                answer = json.loads(await reader.readline())
                writer.close()
                await writer.wait_closed()
                assert not answer["ok"] and answer["error"] == message
            tool = await asyncio.create_subprocess_exec(sys.executable, ENGINE / "jardctl.py", "history", "clear", "--json",
                cwd=ROOT, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await tool.communicate()
            assert tool.returncode == 1 and not out and "Pass --yes" in json.loads(err)["error"]
            await desktop_command(["history", "clear", "--yes"], "history-clear", {}, {"removed": 3, "changed": True})
            await desktop_command(["history", "clear", "--yes"], "history-clear", {}, {"removed": 0, "changed": False})
            history_home = root / "history-home"
            history_file = history_home / "Library/Preferences/local.jardaudio.history-fixture.plist"
            history_file.parent.mkdir(parents=True)
            rows = [{"id": str(i), "time": time.time() - i, "text": f"History entry {i}."} for i in range(300)]
            history_file.write_bytes(plistlib.dumps({"JardSystemDictationHistory": rows[:250]}))
            history_file.chmod(0o600)
            history_env = {**env, "HOME": str(history_home), "JARD_DESKTOP_BUNDLE_ID": history_file.stem}
            before_history = history_file.stat().st_mtime_ns
            def history_snapshot(request):
                nonlocal snapshot_file
                snapshot_file = Path(socket_path).with_name(f"history-{request['id']}.plist")
                assert request["path"] == str(snapshot_file)
                snapshot_file.write_bytes(plistlib.dumps({
                    "bundleId": history_file.stem, "JardSystemDictationHistory": rows,
                }))
                snapshot_file.chmod(0o600)
                return {"path": str(snapshot_file)}
            snapshot_file = None
            await desktop_command(["history", "--query", "entry", "--offset", "290"], "history-checkpoint", history_snapshot,
                True, environment=history_env, expected={"entries": rows[290:], "total": 300, "words": 900, "seconds": 0})
            assert snapshot_file and not snapshot_file.exists()
            assert history_file.stat().st_mtime_ns == before_history
            print("Live CLI paginated a large private history snapshot, removed it, and left offline preferences intact.", flush=True)
            await desktop_command(["window", "show"], "window-show", {"pane": "settings"},
                                  {"windowVisible": True, "requestedPane": "settings"})
            await desktop_command(["window", "show", "history"], "window-show", {"pane": "history"},
                                  {"windowVisible": True, "requestedPane": "history"})
            await desktop_command(["window", "hide"], "window-hide", {}, {"windowVisible": False, "changed": True})
            await desktop_command(["dictation", "start", "--raw"], "dictation-start-raw", {}, {"phase": "starting"})
            await desktop_command(["dictation", "toggle", "--raw"], "dictation-toggle-raw", {}, {"phase": "starting"})
            printed = {"status": "transcript", "text": "This dictation goes to the command."}
            await desktop_command(["dictation", "stop", "--print"], "dictation-stop-print", {}, printed, json_output=False)
            await desktop_command(["dictation", "stop", "--print"], "dictation-stop-print", {}, printed)
            await desktop_command(["dictation", "stop", "--print"], "dictation-stop-print", {}, {"status": "no-speech"}, exit_code=4)
            await desktop_command(["meetings", "status"], "meeting-status", {}, {"phase": "idle", "detection": False})
            await desktop_command(["meetings", "record"], "meeting-record", {}, {"phase": "starting"})
            await desktop_command(["meetings", "record", "--title", "  Design   Review  "], "meeting-record",
                                  {"title": "Design Review"}, {"phase": "starting", "title": "Design Review"})
            await desktop_command(["meetings", "record", "--title", "  "], "meeting-record", {}, {"phase": "starting"})
            annotation_request = str(uuid4())
            await desktop_command(["annotate", "start"], "annotation-start", {},
                                  {"phase": "starting", "capture": annotation_request, "changed": True})
            await desktop_command(["capture", "start", "--source", "area", "--language", "en"], "annotation-start",
                                  {"source": "area", "languages": ["en"]},
                                  {"phase": "picking", "capture": annotation_request, "changed": True})
            await desktop_command(["annotate", "start", "--source", "phone"], "annotation-start",
                                  {"source": "phone"},
                                  {"phase": "starting", "capture": annotation_request, "changed": True})
            await desktop_command(["annotate", "status"], "annotation-status", {},
                                  {"phase": "recording", "capture": annotation_request, "elapsedSeconds": 2.5})
            await desktop_command(["annotate", "stop"], "annotation-stop", {},
                                  {"phase": "saving", "capture": annotation_request, "changed": True})
            await desktop_command(["annotate", "cancel"], "annotation-cancel", {},
                                  {"phase": "saving", "capture": annotation_request, "changed": False})
            reader, writer = await asyncio.open_unix_connection(pointer)
            writer.write(b'{"action":"annotation-start","token":"invalid"}\n')
            await writer.drain()
            denied = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
            assert not denied["ok"] and "credentials" in denied["error"]
            long_clip = root / "two-pieces.wav"
            with wave.open(str(long_clip), "wb") as combined:
                combined.setparams((1, 2, 48_000, 0, "NONE", ""))
                combined.writeframes(remote_samples.astype("<i2").tobytes())
                combined.writeframes(bytes((25 * 48_000 - len(remote_samples)) * 2))
                combined.writeframes(local_samples.astype("<i2").tobytes())
            clip = await asyncio.create_subprocess_exec(sys.executable, ENGINE / "jardctl.py", "transcribe", long_clip.name,
                        "--language", "en", "--json", cwd=root, env=env,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await clip.communicate()
            assert clip.returncode == 0, (out, err)
            text = json.loads(out)["text"].lower()
            assert 0 <= text.find("history books") < text.find("rachel"), text
            print("Warm overlapping takes, ordered profiles, authenticated CLI, and file transcription passed.",flush=True)
            document = {"type":"doc","content":[{"type":"heading","attrs":{"level":2},"content":[
                {"type":"text","text": "Ask about paperback pricing."}]}]}
            draft = (await web.post("/api/personal-note-drafts", json={"document":document})).raise_for_status().json()
            print("Native microphone-only dictation, live preview, final transcription and temporary file cleanup passed.",flush=True)
            first=(await rpc("openAudio", notesDraft=draft["id"], **opts))["id"]
            saved=await completed(first)
            assert saved["personalNotes"] == {"document":document,"revision":1}
            assert saved["transcript"]["words"] and saved["transcript"]["segments"]
            for kind in ("audio","json"):
                assert (await rpc("export", id=first,kind=kind))["saved"]
            assert (root/"export-review.wav").read_bytes()==audio.read_bytes()
            exported=json.loads((root/"export-review.json").read_text())
            assert all(exported[k]==v for k,v in saved["transcript"].items())
            assert exported["modelRevision"]==saved["modelRevision"] and exported["source"]==audio.name
            print("Authenticated bridge, staged import, real Metal models, timed speaker transcript and both native exports passed.",flush=True)

            deny_recording = root / "deny-recording-permissions"
            deny_recording.touch()
            try:
                for source in ("mic", "system"):
                    seen.clear()
                    await rpc("record", mode=source, live=False, **opts)
                    denied = await wait_event("error")
                    assert denied["permission"] == source and not (await rpc("devices"))["drafts"]
            finally:
                deny_recording.unlink()
            print("Typed microphone and system audio denials reach the UI and remove empty staging files.", flush=True)
            await rpc("record",mode="both",live=True,**opts)
            await wait_event("started")
            assert "meetingRms" not in await wait_event("levels")
            await rpc("live",enabled=False)
            await rpc("stop")
            detail=await wait_event("accepted")
            call=await completed(detail["id"])
            assert call["recording"] and call["transcript"]["text"]
            assert "recordingTracks" not in call
            seen.clear()
            meeting = str(uuid4())
            assert (await rpc("warm", keep=True))["warm"]
            await rpc("record", mode="both", meeting=meeting.upper(), meetingLabel="Zoom",
                      meetingApplication="us.zoom.xos", meetingSystemAudio="tapAndScreen",
                      inputDevice="Saved microphone", live=True, **opts)
            started = await wait_event("started")
            assert started["meeting"] == meeting and started["live"] and started["mode"] == "both"
            assert started["microphoneAvailable"] is True
            while len(warnings := [row["detail"]["active"] for row in seen
                                   if row.get("detail", {}).get("event") == "meetingMicWarning"]) < 2:
                await event()
            assert warnings == [True, False]
            while len(system := [row["detail"]["available"] for row in seen
                                 if row.get("detail", {}).get("event") == "meetingSystemAudio"]) < 2:
                await event()
            assert system == [False, True]
            mic_states = [row["detail"]["microphoneAvailable"] for row in seen
                          if row.get("detail", {}).get("event") == "sourceChanged"]
            assert mic_states == [False, True], mic_states
            async with asyncio.timeout(5):
                while not (mic_recovery := root / "us.zoom.xos.mic-recovery").exists():
                    await asyncio.sleep(.05)
            assert mic_recovery.read_text() == "recover-mic"
            for uid in ('Casque "Écoute"', ""):
                await rpc("meetingInput", inputDevice=uid)
                name = uid or "System default"
                while not any(row.get("detail", {}).get("event") == "sourceChanged" and
                              row["detail"].get("microphone") == name for row in seen):
                    await event()
            assert "no longer active" in await rpc("stopMeeting", meeting=str(uuid4()), expect_error=True)
            assert "already active" in await rpc("record", mode="mic", expect_error=True)
            preview = await wait_event("preview")
            assert preview["meeting"] == meeting and preview["delta"] == preview["text"] and preview["delta"]
            assert preview["deltaSegments"][0]["speaker"] > 0, preview

            async def agent(*arguments, request=None, markdown=False):
                tool = await asyncio.create_subprocess_exec(sys.executable, ENGINE / "jardctl.py", *arguments, cwd=ROOT, env=env,
                            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out, err = await asyncio.wait_for(tool.communicate(request), timeout=10)
                assert tool.returncode == 0, err
                return out.decode() if markdown else json.loads(out)

            live = await agent("meetings", "list", "--since", "1h", "--json")
            assert len(live) == 1 and live[0]["status"] == "recording" and live[0]["meetingProvider"] == "Zoom", live
            live_id = live[0]["id"]
            assert started["session"] == live_id
            marker = root / "library" / "staging" / f"{live_id}.recording"
            assert json.loads(marker.read_text())["meetingMicrophoneWarning"] == "digitalSilence"
            assert not await agent("meetings", "list", "--no-in-progress", "--json")
            journal = root / "library" / "staging" / f"{live_id}.rolling.ndjson"
            rolling_bytes = journal.read_bytes()
            window = json.loads(rolling_bytes.splitlines()[0])
            assert window[:2] == [0, 4 * 48000] and "history books" in window[2]["text"].lower(), window
            assert window[2]["meetingSpeakerMode"] == "mixed", window[2]
            readings = await agent("meetings", "show", live_id[:8], "--json")
            assert readings["transcriptSource"] == "rolling" and readings["turns"][0]["speaker"].startswith("Speaker "), readings
            assert readings["microphoneWarning"] == "digitalSilence"
            live_markdown = await agent("meetings", "show", live_id, "--from", "0:00", "--md", markdown=True)
            assert "history books" in live_markdown.lower() and "Speaker " in live_markdown
            assert "- **Range:** 00:00–end" in live_markdown
            assert "- **Status:** Recording · rolling transcript" in live_markdown
            excerpt = await agent("meetings", "show", live_id, "--to", "3.25s", "--json")
            assert excerpt["range"] == {"from_ms": 0, "to_ms": 3250}
            assert excerpt["turns"] == [turn for turn in readings["turns"] if round(turn["start"] * 1000) < 3250]
            mcp_request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_transcripts",
                           "arguments": {"meeting_id": "latest", "query": '"history books"', "since": "1h"}}}
            mcp = await agent("mcp", request=(json.dumps(mcp_request) + "\n").encode())
            hit = json.loads(mcp["result"]["content"][0]["text"])["passages"][0]
            assert hit["id"] == live_id and hit["status"] == "recording" and "**history** **books**" in hit["snippet"].lower(), hit
            assert "no longer active" in await rpc("copyMeeting", meeting=str(uuid4()), expect_error=True)
            snapshot = await rpc("copyMeeting", meeting=meeting.upper())
            assert snapshot == {"through": count - 1200, "complete": True}, snapshot
            excerpts = [row["detail"] for row in seen if row.get("detail", {}).get("meeting") == meeting
                        and row["detail"].get("event") == "preview"]
            assert any(row.get("detail", {}).get("meeting") == meeting
                       and row["detail"].get("meetingRms") == .15 for row in seen)
            assert excerpts[-1]["through"] <= snapshot["through"], excerpts[-1]["through"]
            assert "rachel" in " ".join(row["delta"] for row in excerpts).lower(), excerpts
            native_headers = {"X-Jard-Token": (root / "library" / ".native-token").read_text().strip()}
            assert (await web.put("/api/native/meeting-clock/24")).status_code == 403
            await rpc("meetingClock", hours="24")
            title_route = f"/api/native/staging/{live_id}/meeting-title"
            title_params = {"through": excerpts[-1]["through"]}
            assert (await web.post(title_route, params=title_params)).status_code == 403
            automatic_notes = "/api/meetings/automatic-notes"
            origin = {"Origin": base}
            assert (await web.get(automatic_notes)).raise_for_status().json() == {"enabled": True}
            assert (await web.put(automatic_notes, json={"enabled": False})).status_code == 403
            assert (await web.put(automatic_notes, headers=origin, json={"enabled": "false"})).status_code == 422
            assert (await web.put(automatic_notes, headers=origin, json={"enabled": False})).raise_for_status().json() == {"enabled": False}
            paused = (await web.post(title_route, headers=native_headers, params=title_params)).raise_for_status().json()
            assert paused == {"automatic": False}, paused
            (await web.put(automatic_notes, headers=origin, json={"enabled": True})).raise_for_status()
            title = (await web.post(title_route, headers=native_headers, params=title_params)).raise_for_status().json()["title"]
            assert title and len(title.split()) <= 8, title
            live_title = journal.with_name(f"{live_id}.meeting-title")
            live_title.write_text(title, encoding="utf8")
            fallback = await rpc("copyMeeting", meeting=meeting)
            assert fallback == {"through": snapshot["through"], "complete": False}, fallback
            joined = str(uuid4())
            folder = root / ("jard-annotation-" + joined)
            folder.mkdir(mode=0o700)
            joined_narration = folder / "Narration.wav"
            assert started["warm"] and (await rpc("annotationLease", id=joined, active=True))["active"]
            assert (await rpc("annotationAudio", id=joined, action="prepare", path=str(joined_narration), inputDevice="Different input"))["ready"]
            assert (await rpc("annotationAudio", id=joined, action="start", at=time.monotonic()))["ready"]
            capture = str(uuid4())
            await rpc("record", dictation=capture.upper(), **{"global": True, "keep": True})
            while not any(row.get("detail", {}).get("event") == "started" and
                          row["detail"].get("dictation") == capture for row in seen):
                await event()
            take = next(row["detail"] for row in seen if row.get("detail", {}).get("dictation") == capture)
            assert take["microphone"] == "Fixture microphone" and take["mode"] == "mic" and take["warm"], take
            assert (await rpc("devices"))["capture"]["meeting"] == meeting
            assert "already active" in await rpc("record", dictation=str(uuid4()), **{"global": True}, expect_error=True)
            assert "Finish your recording" in await rpc("updateQuiesce", expect_error=True)
            await rpc("dictation", action="stop", id=capture, releaseTail=True, releaseAt=101.456)
            stage = (await wait_event("dictationReady"))["id"]
            dictated = await rpc("dictation", action="transcribe", id=stage, languages=["en"])
            assert "history books" in dictated["text"].lower(), dictated
            assert type(dictated.get("inferenceMs")) is int and dictated["inferenceMs"] > 0, dictated
            assert (await rpc("devices"))["capture"]["meeting"] == meeting
            cancelled = str(uuid4())
            await rpc("record", dictation=cancelled, **{"global": True, "keep": True})
            await rpc("dictation", action="cancel", id=cancelled)
            assert not any(row.get("detail", {}).get("event") == "dictationReady" and
                           row["detail"].get("dictation") == cancelled for row in seen)
            event_path = "/api/meetings/events"
            event_headers = {"Authorization": "Bearer " + (root / "library" / ".native-token").read_text().strip()}
            assert (await web.get(event_path)).status_code == 401
            assert (await web.get(event_path, headers={**event_headers, "Origin": "https://example.invalid"})).status_code == 403
            async with web.stream("GET", event_path, headers=event_headers) as stream:
                assert stream.status_code == 200 and "text/event-stream" in stream.headers["content-type"]
                assert (await web.get(event_path, headers=event_headers)).status_code == 409
                await rpc("stopMeeting", meeting=meeting.upper())
                finished = await wait_event("finished")
                assert finished["meeting"] == meeting and finished["seconds"] == count / 48_000, finished
                detail = await wait_event("accepted")
                assert detail["id"] == live_id
                assert (await rpc("annotationAudio", id=joined, action="stop", at=time.monotonic()))["ready"]
                assert joined_narration.read_bytes() == audio.read_bytes()
                assert not (await rpc("annotationLease", id=joined, active=False))["active"]
                async with asyncio.timeout(30):
                    async for line in stream.aiter_lines():
                        if line.startswith("data: "):
                            assert json.loads(line[6:]) == {"type": "meeting.finished", "id": live_id}
                            saved_from_event = (await web.get(f"/api/sessions/{live_id}")).raise_for_status().json()
                            assert saved_from_event["status"] == "complete" and saved_from_event["transcript"]["text"]
                            break
                    else:
                        raise AssertionError("The event stream closed before meeting.finished")
            # Closing the client response and observing its disconnect in ASGI
            # can happen on opposite sides of the next connection attempt.
            async with asyncio.timeout(3):
                while True:
                    async with web.stream("GET", event_path, headers=event_headers) as reconnected:
                        if reconnected.status_code == 200:
                            break
                        assert reconnected.status_code == 409, reconnected.status_code
                    await asyncio.sleep(.05)
            assert detail["meeting"] == meeting
            saved_meeting = await completed(detail["id"])
            assert saved_meeting["provisionalTitle"] == title and not live_title.exists()
            assert saved_meeting["recording"] and saved_meeting["audioName"].startswith("Zoom meeting ")
            assert saved_meeting["meeting"] and saved_meeting["meetingProvider"] == "Zoom"
            assert saved_meeting["meetingStartedAt"] == live[0]["meetingStartedAt"]
            start_time = datetime.fromisoformat(saved_meeting["meetingStartedAt"]).astimezone()
            date_line = f"- **Date:** {start_time:%Y-%m-%d}, {start_time:%H:%M}"
            assert date_line in (await web.get(f"/api/sessions/{live_id}/export?kind=notes")).raise_for_status().text
            assert date_line in await agent("meetings", "show", live_id, "--md", markdown=True)
            await rpc("meetingClock", hours="12")
            assert saved_meeting["meetingMicrophoneWarning"] == "digitalSilence"
            assert (await agent("meetings", "show", live_id, "--json"))["microphoneWarning"] == "digitalSilence"
            assert not journal.exists()
            transcript = saved_meeting["transcript"]
            assert transcript["text"].lower().count("history books") == 1, transcript
            assert "rachel" in transcript["text"].lower()
            assert "speakerNames" not in transcript and saved_meeting["diarization"] and transcript["speakerActivity"]
            copied = await rpc("latestMeetingTranscript")
            assert copied["session"] == live_id and "history books" in copied["text"].lower()
            assert " · Speaker " in copied["text"]
            picked = await rpc("savedMeetingTranscript", id=live_id)
            assert "history books" in picked["text"].lower() and " · Speaker " in picked["text"]
            assert picked["text"] == (await web.get(f"/api/sessions/{live_id}/transcript-clipboard")).raise_for_status().text
            assert "no longer available" in await rpc("savedMeetingTranscript", id=str(uuid4()), expect_error=True)
            assert (await web.get(f"/api/native/meetings/{live_id}/excerpt")).status_code == 403
            excerpt = await rpc("meetingExcerpt", session=live_id)
            assert (await rpc("latestMeeting"))["session"] == live_id
            assert excerpt["available"] and excerpt["settled"] and excerpt["labels"] == {}
            assert {turn["speaker"] for turn in excerpt["turns"]} == {turn["speaker"] for turn in transcript["segments"]}
            assert all(0 <= turn["start"] <= turn["end"] for turn in excerpt["turns"])
            assert await rpc("meetingExcerpt", session=str(uuid4())) == {"available": False, "settled": True, "turns": []}
            assert set(saved_meeting["recordingTracks"]) == {"mic", "system"}
            mic = (await web.get(saved_meeting["recordingTracks"]["mic"])).raise_for_status().content
            system_audio = (await web.get(saved_meeting["recordingTracks"]["system"])).raise_for_status().content
            assert mic == Path(env["MEETING_MIC"]).read_bytes() and system_audio == Path(env["MEETING_SYSTEM"]).read_bytes()
            fallback = await web.post("/api/playback", params={"saved": detail["id"], "track": "mic"},
                                      headers={"Content-Type": "application/octet-stream"})
            assert fallback.raise_for_status().headers["content-type"] == "audio/mp4"
            key = detail["id"]
            async with asyncio.timeout(95):
                while True:
                    brief = (await web.get(f"/api/sessions/{key}/meeting-brief")).raise_for_status().json()
                    if brief["status"] != "queued":
                        break
                    await asyncio.sleep(.2)
            assert brief["status"] == "ready" and brief["title"] and brief["summary"], brief
            saved_summary = (await web.get(f"/api/sessions/{key}")).raise_for_status().json()
            assert saved_summary["name"] == brief["title"] and saved_summary["transcript"] == transcript
            exported_summary = (await web.get(f"/api/sessions/{key}/export?kind=md")).raise_for_status().text
            assert brief["summary"] in exported_summary and exported_summary.index("## Summary") < exported_summary.index("## Transcript")
            assert (await web.get(f"/api/native/meetings/{key}/completion")).status_code == 403
            completion = await rpc("meetingCompletion", session=key)
            assert completion["status"] == "complete" and completion["hasSpeech"] and completion["title"] == brief["title"], completion
            assert completion["summaryStatus"] == "ready" and completion["summary"] == brief["summary"], completion
            assert "transcript" not in completion and "turns" not in completion
            assert await rpc("meetingMarkdown", session=key) == {"text": exported_summary}
            print("Rumble2 Flash meeting finalization queued a local Qwen summary and included it in Markdown.", flush=True)
            result = (await web.get("/api/sessions", params={"meetings": "true", "q": "Zoom"})).raise_for_status().json()
            assert [row["id"] for row in result] == [key]
            revision = (await web.put(f"/api/sessions/{key}/personal-notes", json={"revision": 0, "document": document})).raise_for_status().json()
            assert revision["revision"] == 1
            image = subprocess.check_output(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:size=16x16",
                                             "-frames:v", "1", "-f", "image2pipe", "-c:v", "mjpeg", "-"])
            images_path = f"/api/sessions/{key}/images"
            upload_headers = {"Content-Type": "image/jpeg", "Origin": base}
            assert (await web.post(images_path, content=b"not an image", headers=upload_headers)).status_code == 422
            assert (await web.post(images_path, content=bytes(512 * 1024 + 1), headers=upload_headers)).status_code == 413
            asset = (await web.post(images_path, content=image, headers=upload_headers)).raise_for_status().json()
            assert (await web.get(images_path)).raise_for_status().json() == [asset]
            response = (await web.get(asset["url"])).raise_for_status()
            assert response.content == image and response.headers["content-type"] == "image/jpeg"
            for provider, target in agent_targets.items():
                result = await rpc("sendSessionToAgent", id=key, agent=provider, target=target)
                wire = json.loads((root / f"{provider}-annotation.json").read_text())
                context = wire if provider == "t3" else wire[0]["text"]
                assert brief["summary"] in context and "## My Notes" in context and "Ask about paperback pricing." in context
                assert "## Transcript" in context and "**00:" in context
                if provider == "t3":
                    assert result == {"images": "references", "opened": False} and base + asset["url"] in context
                else:
                    assert result == {"images": "attached", "opened": False}
                    encoded = wire[1]["url"].split(",", 1)[1] if provider == "codex" else wire[1]["source"]["data"]
                    assert base64.b64decode(encoded) == image
            with local_opencode(Path(env["XDG_STATE_HOME"]) / "opencode") as (_, _, prompts, _):
                choice = await rpc("annotationDestinations", query="")
                result = await rpc("sendSessionToAgent", id=key, agent="opencode", target="ses_project", service=choice["service"])
                assert result == {"images": "attached", "opened": False}
                path, body = prompts[-1]
                assert path == "/api/session/ses_project/prompt" and "Ask about paperback pricing." in body["text"]
                assert base64.b64decode(body["files"][0]["uri"].split(",", 1)[1]) == image
            (await web.delete(asset["url"], headers={"Origin": base})).raise_for_status()
            assert (await web.get(asset["url"])).status_code == 404
            print("Overview sharing preserved a saved meeting brief, timed transcript, My Notes, and image attachments across all four agent transports.", flush=True)
            for kind, expected in (("notes", "Ask about paperback pricing."), ("md", "**"), ("srt", " --> "), ("vtt", "WEBVTT")):
                result = (await web.get(f"/api/sessions/{key}/export", params={"kind": kind})).raise_for_status()
                assert expected in result.text and "attachment" in result.headers["content-disposition"]
                assert (await rpc("export", id=key, kind=kind))["saved"]
            assert (await web.get("/api/meetings/audio-retention")).json() == {"days": None}
            assert (await web.get("/api/meetings/retention")).json() == {"days": None}
            assert (await web.put("/api/meetings/retention", params={"days": 0})).status_code == 403
            assert (await web.put("/api/meetings/retention", params={"days": 0},
                                  headers={"Origin": base})).raise_for_status().json() == {"days": 0}
            assert (await web.put("/api/meetings/audio-retention?days=7")).raise_for_status().json() == {"days": 7}
            removed = (await web.delete(f"/api/sessions/{key}/audio")).raise_for_status().json()["audioRemovedAt"]
            trashed_audio = root / "Trash" / f"Jaro Meeting Audio {key}"
            assert (trashed_audio / "mic.wav").read_bytes() == mic
            assert (trashed_audio / "system.wav").read_bytes() == system_audio
            assert (await web.get(f"/api/sessions/{key}/audio?track=mic")).status_code == 404
            archived = (await web.get(f"/api/sessions/{key}")).raise_for_status().json()
            assert archived["audioRemovedAt"] == removed and archived["audioUrl"] is None
            assert archived["transcript"] == transcript and archived["personalNotes"]["revision"] == 1
            assert await rpc("latestMeetingTranscript") == copied
            assert not list((root/"library"/"staging").glob("*.wav"))
            print("Dual-source meeting recording, Rumble2 Flash copy-through-now and timeout fallback, and saved session passed.", flush=True)
            seen.clear()
            assert not (await rpc("warm", keep=False))["warm"]
            capture, meeting = str(uuid4()), str(uuid4())
            await rpc("record", dictation=capture, **{"global": True})
            calendar = {"title": "Design sync / review", "attendees": ["Riley", "Jordan"]}
            settings_route = "/api/native/meeting-exports"
            assert await rpc("meetingExports", action="status") == {"folder": None, "separateNotes": False, "descriptiveNames": False}
            assert (await web.put(settings_route, json={"folder": str(export_folder)})).status_code == 403
            assert (await web.put(settings_route, headers=native_headers, json={"folder": str(root / "library")})).status_code == 400
            assert await rpc("meetingExports", action="choose") == {"folder": str(export_folder), "separateNotes": False, "descriptiveNames": False}
            assert (await rpc("meetingExports", action="separateNotes", enabled=True))["separateNotes"] is True
            user_title = "Design handoff / launch"
            await rpc("record", mode="both", meeting=meeting, live=True, meetingCalendar=calendar, meetingTitle=user_title,
                      meetingApplication="com.microsoft.teams2", **opts)
            async with asyncio.timeout(5):
                while not (mic_recovery := root / "com.microsoft.teams2.mic-recovery").exists():
                    await asyncio.sleep(.05)
            assert mic_recovery.read_text() == "defer-mic"
            (await web.put(automatic_notes, headers=origin, json={"enabled": False})).raise_for_status()
            assert await rpc("copyMeeting", meeting=meeting) == {"through": 0, "complete": False}
            assert not any(row.get("detail", {}).get("event") == "preview" for row in seen)
            [current] = await agent("meetings", "list", "--query", user_title, "--json")
            assert current["status"] == "recording", current
            assert current["name"] == user_title and current["meetingCalendar"] == calendar
            marker = root / "library" / "staging" / (current["id"] + ".recording")
            assert json.loads(marker.read_text())["meetingTitle"] == user_title
            await rpc("stopMeeting", meeting=meeting)
            accepted = await wait_event("accepted")
            assert accepted["meeting"] == meeting
            assert (await rpc("devices"))["capture"] is None
            assert "Finish your recording" in await rpc("updateQuiesce", expect_error=True)
            await rpc("dictation", action="stop", id=capture, releaseTail=True)
            stage = (await wait_event("dictationReady"))["id"]
            dictated = await rpc("dictation", action="transcribe", id=stage, languages=["en"])
            assert "history books" in dictated["text"].lower()
            saved_calendar = await completed(accepted["id"])
            assert set(saved_calendar["recordingTracks"]) == {"mic", "system"}
            assert saved_calendar["meetingCalendar"] == calendar and saved_calendar["name"] == user_title
            assert saved_calendar["meetingTitle"] == user_title and "provisionalTitle" not in saved_calendar
            assert saved_calendar["meetingBrief"] is None, saved_calendar["meetingBrief"]
            manual = (await web.post(f"/api/sessions/{accepted['id']}/meeting-brief", headers=origin)).raise_for_status().json()
            assert manual["status"] == "queued", manual
            (await web.put(automatic_notes, headers=origin, json={"enabled": True})).raise_for_status()
            print("Automatic meeting titles and summaries respected the live preference and kept manual summaries available.", flush=True)
            exported_calendar = await web.get(f"/api/sessions/{accepted['id']}/export?kind=md")
            assert f"# {user_title}" in exported_calendar.raise_for_status().text and "Riley, Jordan" in exported_calendar.text
            export_endpoint = f"/api/sessions/{accepted['id']}/managed-export"
            async with asyncio.timeout(10):
                while (managed := (await web.get(export_endpoint)).raise_for_status().json())["status"] == "pending":
                    await asyncio.sleep(.1)
            assert managed["status"] == "ready" and managed["filename"].endswith("Design handoff - launch.transcript.md"), managed
            path = export_folder / managed["filename"]
            assert "Riley, Jordan" in path.read_text() and "history books" in path.read_text().lower()
            locate = (await web.get(f"{settings_route}/{accepted['id']}", headers=native_headers)).raise_for_status()
            assert locate.json()["path"] == str(path)
            assert await rpc("meetingExports", action="disable") == {"folder": None, "separateNotes": True, "descriptiveNames": False}
            assert path.is_file()
            async with asyncio.timeout(95):
                while not (managed := (await web.get(export_endpoint)).raise_for_status().json())["notesFilename"]:
                    await asyncio.sleep(.25)
            notes_path = export_folder / managed["notesFilename"]
            assert notes_path.name == path.name.replace(".transcript.md", ".notes.md")
            brief = (await web.get(f"/api/sessions/{accepted['id']}/meeting-brief")).raise_for_status().json()
            assert brief["summary"] in path.read_text() and brief["summary"] in notes_path.read_text()
            assert (await rpc("meetingCompletion", session=accepted["id"]))["title"] == user_title
            assert "[Open transcript](" in notes_path.read_text()
            assert (await rpc("meetingExports", action="descriptiveNames", enabled=True))["descriptiveNames"] is True
            title_route = f"/api/sessions/{accepted['id']}/meeting-title"
            assert (await web.put(title_route, json={"title": "Unsigned"})).status_code == 403
            renamed = (await web.put(title_route, headers={"Origin": base}, json={"title": "Design handoff"})).raise_for_status().json()
            assert renamed["title"] == "Design handoff" and renamed["export"]["filename"].endswith("Design handoff.transcript.md"), renamed
            reveal = (await web.get(f"/api/native/meeting-exports/{accepted['id']}", headers=native_headers)).raise_for_status().json()
            assert Path(reveal["path"]).name == renamed["export"]["filename"]
            assert not path.exists() and not notes_path.exists()
            assert (await rpc("meetingExports", action="separateNotes", enabled=False))["separateNotes"] is False
            print("Local Qwen refreshed the selected Markdown transcript and published its linked companion notes.", flush=True)
            assert not list((root/"library"/"staging").glob("*.wav"))
            print("Cold and warm global dictations ran beside meetings. Stopping either take left the other active.", flush=True)
            meeting_stage = (await web.post("/api/native/staging?recording=true&meeting=true&provider=Zoom",
                                            headers=native_headers, json=calendar)).raise_for_status().json()
            regular_stage = (await web.post("/api/native/staging?recording=true", headers=native_headers)).raise_for_status().json()
            for draft in (meeting_stage, regular_stage):
                Path(draft["path"]).write_bytes(audio.read_bytes())
            recovered_rolling = Path(meeting_stage["path"]).with_suffix(".rolling.ndjson")
            recovered_rolling.write_bytes(rolling_bytes + b'[192000,')
            recovered_title = Path(meeting_stage["path"]).with_suffix(".meeting-title")
            recovered_title.write_text(title, encoding="utf8")
            assert await rpc("meetingDrafts") == [{"id": meeting_stage["id"], "provider": "Zoom"}]
            assert "unavailable" in await rpc("recoverMeeting", id=regular_stage["id"], expect_error=True)
            draft_detail = await agent("meetings", "show", meeting_stage["id"], "--json")
            assert draft_detail["staged"] and draft_detail["status"] == "interrupted" and draft_detail["transcriptSource"] == "rolling", draft_detail
            assert draft_detail["provisionalTitle"] == title and draft_detail["title"] == calendar["title"]
            assert "history books" in draft_detail["turns"][0]["text"].lower(), draft_detail
            marker = Path(meeting_stage["path"]).with_suffix(".recording")
            metadata = json.loads(marker.read_text())
            metadata["meetingTitle"] = "Recovered workshop"
            metadata["meetingMicrophoneWarning"] = "digitalSilence"
            marker.write_text(json.dumps(metadata), encoding="utf8")
            assert await rpc("meetingDrafts") == [{"id": meeting_stage["id"], "provider": "Zoom", "title": "Recovered workshop"}]
            draft_detail = await agent("meetings", "show", meeting_stage["id"], "--json")
            assert draft_detail["title"] == "Recovered workshop" and draft_detail["microphoneWarning"] == "digitalSilence"
            seen.clear()
            recovered = await rpc("recoverMeeting", id=meeting_stage["id"], languages=["en"])
            assert (await wait_event("accepted"))["id"] == recovered["id"] == meeting_stage["id"]
            recovered_session = await completed(recovered["id"])
            assert recovered_session["meetingCalendar"] == calendar and recovered_session["meetingProvider"] == "Zoom"
            assert recovered_session["name"] == recovered_session["meetingTitle"] == "Recovered workshop"
            assert recovered_session["meetingMicrophoneWarning"] == "digitalSilence"
            assert recovered_session["provisionalTitle"] == title and not recovered_title.exists()
            assert "history books" in recovered_session["transcript"]["text"].lower()
            assert recovered_session["diarization"] and "speakerNames" not in recovered_session["transcript"]
            assert recovered_session["transcript"]["text"].lower().count("history books") == 1
            assert not recovered_rolling.exists()
            assert not await rpc("meetingDrafts")
            await rpc("discard", id=regular_stage["id"])
            print("The local Qwen title route and Rumble2 recording import preserved provisional titles across normal and interrupted saves.", flush=True)
            annotation = str(uuid4())
            assert not (await agent("annotate", "storage"))["custom"]
            configured = await agent("annotate", "storage", "--folder", str(annotation_storage))
            assert configured["custom"] and configured["available"] and configured["folder"] == str(annotation_storage)
            assert not (await rpc("warm", keep=False))["warm"]
            seen.clear()
            earlier_dictation = str(uuid4())
            await rpc("record", dictation=earlier_dictation, keep=False, inputDevice="FixtureWarmReady", **{"global": True})
            started = await wait_event("started")
            assert started["dictation"] == earlier_dictation and started["warm"]
            assert (await rpc("annotationLease", id=annotation, active=True))["active"]
            assert "Finish the active annotation" in await rpc("annotationStorage", action="reset", expect_error=True)
            assert "screen annotation" in await rpc("record", mode="both", expect_error=True)
            folder = root / ("jard-annotation-" + annotation)
            folder.mkdir(mode=0o700)
            narration = folder / "Narration.wav"
            assert (await rpc("annotationAudio", id=annotation, action="prepare", path=str(narration), inputDevice="Do not switch during dictation"))["ready"]
            assert (await rpc("annotationAudio", id=annotation, action="start", at=time.monotonic()))["ready"]
            assert not any(row.get("detail", {}).get("event") == "dictationReady" for row in seen)
            await rpc("dictation", action="stop", id=earlier_dictation)
            recorded = await wait_event("dictationReady")
            assert recorded["dictation"] == earlier_dictation
            spoken = await rpc("dictation", action="transcribe", id=recorded["id"], languages=["en"], cleanupOnFailure=True)
            assert "history books" in spoken["text"].lower()
            seen.clear()
            shared_meeting = str(uuid4())
            await rpc("record", mode="both", meeting=shared_meeting, meetingLabel="Shared meeting", live=True, languages=["en"])
            started = await wait_event("started")
            assert started["meeting"] == shared_meeting and started["microphone"] == "Shared fixture microphone" and started["warm"]
            assert (await wait_event("meetingSystemAudio"))["available"]
            assert (await rpc("annotationAudio", id=annotation, action="stop", at=time.monotonic()))["ready"]
            assert not (await rpc("annotationLease", id=annotation, active=False))["active"]
            await rpc("warm", keep=False)
            annotation = str(uuid4())
            folder = root / ("jard-annotation-" + annotation)
            folder.mkdir(mode=0o700)
            narration = folder / "Narration.wav"
            assert (await rpc("annotationLease", id=annotation, active=True))["active"]
            assert (await rpc("annotationAudio", id=annotation, action="prepare", path=str(narration), inputDevice="Do not switch during meeting"))["ready"]
            assert (await rpc("annotationAudio", id=annotation, action="start", at=time.monotonic()))["ready"]
            assert json.loads((root / "warm-options").read_text()) == ["--on-demand", "--input", "FixtureWarmReady"]
            await rpc("meetingInput", inputDevice="Fixture shared input")
            assert (await wait_event("sourceChanged"))["microphone"] == "Fixture shared input"
            assert (await wait_event("preview"))["meeting"] == shared_meeting
            assert (await rpc("copyMeeting", meeting=shared_meeting))["complete"]
            seen.clear()
            overlapping = str(uuid4())
            await rpc("record", dictation=overlapping, keep=False, **{"global": True})
            shared = await wait_event("started")
            assert shared["dictation"] == overlapping and shared["microphone"] == "Fixture microphone" and shared["warm"]
            await rpc("stopMeeting", meeting=shared_meeting)
            shared_record = await wait_event("accepted")
            saved_shared = await completed(shared_record["id"])
            assert set(saved_shared["recordingTracks"]) == {"mic", "system"} and "history books" in saved_shared["transcript"]["text"].lower()
            assert not any(row.get("detail", {}).get("event") == "dictationReady" for row in seen)
            await rpc("dictation", action="stop", id=overlapping)
            recorded = await wait_event("dictationReady")
            assert recorded["dictation"] == overlapping
            spoken = await rpc("dictation", action="transcribe", id=recorded["id"], languages=["en"], cleanupOnFailure=True)
            assert "history books" in spoken["text"].lower()
            seen.clear()
            overlapping = str(uuid4())
            await rpc("record", dictation=overlapping, keep=False, **{"global": True})
            assert (await wait_event("started"))["dictation"] == overlapping
            assert (await rpc("annotationAudio", id=annotation, action="stop", at=time.monotonic()))["ready"]
            assert not any(row.get("detail", {}).get("event") == "dictationReady" for row in seen)
            await rpc("dictation", action="stop", id=overlapping)
            recorded = await wait_event("dictationReady")
            assert recorded["dictation"] == overlapping
            await rpc("dictation", action="transcribe", id=recorded["id"], languages=["en"], cleanupOnFailure=True)
            movie = folder / "Screen annotation.mp4"
            renderer = os.environ.get("JARD_TEST_WINDOW_RENDER")
            phone_fixture = bool(renderer and os.environ.get("JARD_TEST_PHONE_ANNOTATION"))
            capture_name = "Raw fixture.mov" if phone_fixture else "Raw fixture.mp4"
            captured = folder / capture_name if renderer else movie
            dimensions = "90x160" if phone_fixture else "160x90"
            with wave.open(str(audio), "rb") as fixture_narration:
                seconds = fixture_narration.getnframes() / fixture_narration.getframerate()
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=gray:s={dimensions}:r=12",
                            "-t", str(seconds), "-an", "-c:v", "mpeg4", captured], check=True)
            if renderer:
                subprocess.run([renderer, captured, movie], check=True)
            manifest_file = folder / "annotation.json"
            sheet = folder / "contact-sheet.png"
            if not renderer:
                manifest_file.write_text(json.dumps({"version": 1, "source": "display", "coordinateSpace": "normalized-bottom-left",
                    "events": [{"time": .3, "stroke": 1, "from": [.2, .3], "to": [.7, .5], "liftedAt": .45},
                               {"time": 1.2, "clear": True}],
                    "frames": [.25, 1.3], "origin": {"app": "com.fixture.Browser", "name": "Jard", "site": "example.test"}}))
                sheet.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jL1sAAAAASUVORK5CYII="))
            manifest_bytes = manifest_file.read_bytes()
            invalid = json.loads(manifest_bytes)
            assert invalid["events"][0]["liftedAt"] >= invalid["events"][0]["time"]
            invalid["events"][0]["liftedAt"] = .01
            manifest_file.write_text(json.dumps(invalid))
            assert "Invalid annotation stroke release time" in await rpc(
                "importAnnotation", id=annotation, path=str(movie), narration=str(narration), assets=True, expect_error=True)
            invalid = json.loads(manifest_bytes)
            invalid["events"][0]["from"] = [2, 1]
            manifest_file.write_text(json.dumps(invalid))
            assert "Invalid annotation stroke" in await rpc("importAnnotation", id=annotation, path=str(movie), narration=str(narration),
                                                           assets=True, expect_error=True)
            manifest_file.write_bytes(manifest_bytes)
            picked = await rpc("importAnnotation", id=annotation, path=str(movie), narration=str(narration), languages=["en"], assets=True)
            assert picked["id"] == annotation
            ready_handoff = await rpc("annotationHandoff", id=picked["id"], waitForNarration=True)
            assert "history books" in ready_handoff["markdown"].lower()
            assert "already been saved" in await rpc("importAnnotation", id=annotation, path=str(movie), assets=True, expect_error=True)
            screen_session = await completed(picked["id"])
            assert screen_session["audioName"] == movie.name
            assert screen_session["annotationCustomStorage"] and screen_session["audioPath"] is None
            saved_movie = (await web.get(screen_session["audioUrl"])).raise_for_status().content
            assert saved_movie and saved_movie != movie.read_bytes()
            assert "history books" in screen_session["transcript"]["text"].lower()
            assets = screen_session["annotation"]
            assert assets["source"] == ("phone" if phone_fixture else "window" if renderer else "display")
            assert assets["strokeCount"] > 0 and assets["frames"] == json.loads(manifest_bytes)["frames"]
            assert (await web.get(assets["manifestUrl"])).raise_for_status().content == manifest_bytes
            assert (await web.get(assets["contactSheetUrl"])).raise_for_status().content == sheet.read_bytes()
            assert (await rpc("export", id=picked["id"], kind="annotation-manifest"))["saved"]
            assert (root / "export-Screen annotation-annotation.json").read_bytes() == manifest_bytes
            assert (await rpc("export", id=picked["id"], kind="annotation-contact-sheet"))["saved"]
            assert (root / "export-Screen annotation-contact-sheet.png").read_bytes() == sheet.read_bytes()
            detail = await agent("annotate", "show", "latest")
            assert detail["id"] == picked["id"] and "history books" in detail["transcript"].lower()
            assert Path(detail["videoPath"]).parents[1] == annotation_storage
            tracks = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                "stream=codec_type,start_time,duration", "-of", "json", detail["videoPath"]]))["streams"]
            assert {track["codec_type"] for track in tracks} == {"audio", "video"}
            assert all(abs(float(track["start_time"])) < .04 for track in tracks)
            assert abs(float(tracks[0]["duration"]) - float(tracks[1]["duration"])) < .15
            assert Path(detail["manifestPath"]).read_bytes() == manifest_bytes
            assert Path(detail["contactSheetPath"]).read_bytes() == sheet.read_bytes() and detail["frames"] == assets["frames"]
            handoff = await rpc("annotationHandoff", id=picked["id"])
            assert "history books" in handoff["markdown"].lower() and "Contact sheet times" in handoff["markdown"]
            assert ready_handoff == handoff
            assert Path(handoff["imagePath"]).read_bytes() == sheet.read_bytes()
            assert await rpc("annotationReview", id=picked["id"]) == {"imagePath": handoff["imagePath"]}
            for backend, target in agent_targets.items():
                rows = (await rpc("annotationAgentDestinations", agent=backend, query="agent"))["sessions"]
                assert rows == [{"id": target, "title": "Disposable agent chat", "directory": str(root)}]
                delivery = await rpc("sendAnnotationToAgent", id=annotation, agent=backend, target=target)
                wire = json.loads((root / (backend + "-annotation.json")).read_text())
                if backend == "t3":
                    assert delivery["contactSheet"] == "reference"
                    assert handoff["markdown"] in wire and json.dumps(handoff["imagePath"]) in wire
                else:
                    assert delivery["contactSheet"] == "attached" and wire[0]["text"] == handoff["markdown"]
                    inline = wire[1]["url"].split(",", 1)[1] if backend == "codex" else wire[1]["source"]["data"]
                    assert base64.b64decode(inline) == sheet.read_bytes()
            full_annotation = await rpc("sendSessionToAgent", id=annotation, agent="codex", target=agent_targets["codex"])
            assert full_annotation["images"] == "attached"
            context, attachment = json.loads((root / "codex-annotation.json").read_text())
            assert "## Screen annotation" in context["text"] and "Contact sheet times" in context["text"]
            assert base64.b64decode(attachment["url"].split(",", 1)[1]) == sheet.read_bytes()
            assert "Codex declined" in await rpc("sendAnnotationToAgent", id=annotation, agent="codex",
                                                 target=str(uuid4()), expect_error=True)
            print("Explicit agent handoff sent the selected chat the saved narration and included its contact sheet.", flush=True)
            assert "contact sheet is unavailable" in await rpc("annotationReview", id=str(uuid4()), expect_error=True)
            assert (await agent("annotate", "context", picked["id"], "--json"))["markdown"] == handoff["markdown"]
            cancel_export = root / "cancel-export"
            cancel_export.touch()
            assert await rpc("export", id=picked["id"], kind="audio") is None
            assert not (root / "export-Screen annotation.mp4").exists()
            cancel_export.unlink()
            assert (await rpc("export", id=picked["id"], kind="audio"))["saved"]
            assert (root / "export-Screen annotation.mp4").read_bytes() == saved_movie
            assert not (await rpc("annotationLease", id=annotation, active=False))["active"]
            assert not (await rpc("warm", keep=False))["warm"]
            detached = annotation_storage.with_name(annotation_storage.name + " detached")
            annotation_storage.rename(detached)
            try:
                assert not (await agent("annotate", "storage"))["available"]
                assert "Annotations folder is unavailable" in await rpc("annotationLease", id=str(uuid4()), active=True, expect_error=True)
                unavailable = (await web.get(f"/api/sessions/{annotation}")).raise_for_status().json()
                assert unavailable["annotationStorageUnavailable"] and unavailable["audioUrl"] is None
                assert unavailable["transcript"]["text"] == screen_session["transcript"]["text"]
                fallback = await rpc("annotationHandoff", id=annotation)
                assert fallback["imagePath"] is None and fallback["markdown"] == handoff["markdown"]
                assert "contact sheet is unavailable" in await rpc("annotationReview", id=annotation, expect_error=True)
                assert not (await agent("annotate", "storage", "--default"))["custom"]
            finally:
                detached.rename(annotation_storage)
            assert (await agent("annotate", "show", annotation))["videoPath"] == detail["videoPath"]
            print("Annotation and dictation shared one warm helper and stopped independently. The remuxed narration transcribed and survived reconnect.", flush=True)
            no_picture = str(uuid4())
            folder = root / ("jard-annotation-" + no_picture)
            folder.mkdir(mode=0o700)
            narration = folder / "Narration.wav"
            assert (await rpc("annotationLease", id=no_picture, active=True))["active"]
            await rpc("annotationAudio", id=no_picture, action="prepare", path=str(narration), inputDevice="FixtureWarmReady")
            await rpc("annotationAudio", id=no_picture, action="start", at=time.monotonic())
            await rpc("annotationAudio", id=no_picture, action="stop", at=time.monotonic())
            (folder / "annotation.json").write_text(json.dumps({"version": 1, "source": "display",
                "coordinateSpace": "normalized-bottom-left", "missingVideo": "noFrames", "frames": [],
                "events": [{"time": .3, "stroke": 1, "from": [.2, .3], "to": [.7, .5]}]}))
            voice = await rpc("importAnnotation", id=no_picture, path=str(narration), narrationOnly=True, assets=True, languages=["en"])
            assert not (await rpc("annotationLease", id=no_picture, active=False))["active"]
            voice_session = await completed(voice["id"])
            assert voice_session["annotation"]["missingVideo"] == "noFrames" and voice_session["annotation"]["strokeCount"] == 1
            assert not voice_session["annotation"]["frames"] and voice_session["annotation"]["contactSheetUrl"] is None
            assert "history books" in voice_session["transcript"]["text"].lower()
            assert (await web.get(voice_session["audioUrl"])).raise_for_status().content == narration.read_bytes()
            voice_detail = await agent("annotate", "show", no_picture)
            assert voice_detail["videoPath"] is None and Path(voice_detail["audioPath"]).is_file()
            voice_handoff = await rpc("annotationHandoff", id=no_picture)
            assert voice_handoff["imagePath"] is None and "produced no video frames" in voice_handoff["markdown"]
            (await web.delete(f"/api/sessions/{no_picture}")).raise_for_status()
            print("Annotation without video saved its narration WAV and strokes, transcribed with Rumble2 Flash, and shared context without images.", flush=True)
            seen.clear()
            await rpc("record", mode="mic", live=True, **opts)
            started = await wait_event("started")
            assert started["mode"] == "mic" and started["microphone"] == "Fixture microphone"
            assert "meeting" not in started and "warm" not in started
            levels = await wait_event("levels")
            assert levels["mic"] > 0 and levels["system"] == 0 and "meetingRms" not in levels
            await rpc("stop")
            mic_session = await completed((await wait_event("accepted"))["id"])
            assert mic_session["recording"] and not mic_session.get("meeting") and mic_session["diarization"]
            assert "recordingTracks" not in mic_session
            assert "history books" in mic_session["transcript"]["text"].lower()
            assert mic_session["transcript"]["speakerActivity"]
            assert (await web.get(mic_session["audioUrl"])).raise_for_status().content == audio.read_bytes()
            (await web.delete(f"/api/sessions/{mic_session['id']}")).raise_for_status()
            print("A microphone-only session after the shared meeting captured, transcribed, and diarized its original audio.", flush=True)
            state = Path(env["XDG_STATE_HOME"]) / "opencode"
            assert "running locally" in await rpc("annotationDestinations", expect_error=True, query="")
            with local_opencode(state) as (registration, requests, posted, outcome):
                desc = json.loads(registration.read_text())
                registration.write_text(json.dumps({**desc, "pid": 2}))
                assert "running locally" in await rpc("annotationDestinations", expect_error=True, query="")
                registration.write_text(json.dumps(desc))
                registration.chmod(0o644)
                assert "running locally" in await rpc("annotationDestinations", expect_error=True, query="")
                assert not requests
                registration.chmod(0o600)
                destinations = await rpc("annotationDestinations", query="")
                assert [item["id"] for item in destinations["sessions"]] == ["ses_project", "ses_later"]
                assert (await agent("annotate", "destinations", "--query", "planning"))["sessions"] == [
                    {"id": "ses_later", "title": "Roadmap", "directory": "/fixture/Planning"}]
                service = destinations["service"]
                if not phone_fixture:
                    assert detail["origin"] == {"app": "com.fixture.browser", "name": "Jard", "site": "example.test"}
                    assert "example.test" not in handoff["markdown"]
                    initial = await agent("annotate", "destinations", "--capture", "latest")
                    assert initial["rule"] is None and initial["suggestion"] == {
                        "sessionId": "ses_project", "reason": "Project matches the captured app"}
                    app_rule = await agent("annotate", "remember", "latest", "--scope", "app", "--to", "ses_later", "--service", service)
                    app_pick = await rpc("annotationDestinations", id=annotation)
                    assert app_pick["sessions"][0]["id"] == "ses_later" and app_pick["rule"]["id"] == app_rule["id"]
                    assert app_pick["suggestion"]["reason"] == "Saved for Jard"
                    site_rule = await rpc("rememberAnnotationDestination", id=annotation, scope="site", target="ses_project", service=service)
                    rule_pick = await agent("annotate", "destinations", "--capture", annotation)
                    assert rule_pick["rule"]["id"] == site_rule["id"] and rule_pick["rule"]["available"]
                    assert rule_pick["suggestion"] == {"sessionId": "ses_project", "reason": "Saved for example.test"}
                    assert (await agent("annotate", "forget", annotation, "--rule", site_rule["id"]))["forgotten"]
                    outcome["retired_session"] = True
                    offline_rule = await rpc("annotationDestinations", id=annotation)
                    assert offline_rule["rule"]["id"] == app_rule["id"] and not offline_rule["rule"]["available"]
                    outcome["retired_session"] = False
                    registration.chmod(0o644)
                    disconnected = await rpc("annotationDestinations", id=annotation)
                    assert disconnected["warning"] and disconnected["service"] is None
                    assert disconnected["rule"]["id"] == app_rule["id"] and not disconnected["rule"]["available"]
                    registration.chmod(0o600)
                    replacement = await rpc("rememberAnnotationDestination", id=annotation, scope="app", target="ses_project", service=service)
                    assert not (await rpc("forgetAnnotationDestination", id=annotation, ruleId=app_rule["id"]))["forgotten"]
                    assert (await rpc("forgetAnnotationDestination", id=annotation, ruleId=replacement["id"]))["forgotten"]
                    assert not posted and all(method == "GET" for method, _ in requests)
                assert "no longer available" in await rpc("sendAnnotation", expect_error=True, id=annotation,
                                                          target="ses_project", service="0" * 64)
                assert "no longer available" in await rpc("sendAnnotation", expect_error=True, id=annotation,
                                                          target="ses_deleted", service=service)
                assert not posted
                receipt = await agent("annotate", "send", "latest", "--to", "ses_project", "--service", service)
                assert receipt["annotation"] == annotation and receipt["session"] == destinations["sessions"][0]
                assert receipt["message"] == "msg_fixture" and receipt["includedSheet"] and receipt["canUndo"]
                assert receipt["delivery"] == receipt["status"] == "queued"
                key = receipt["deliveryId"]
                assert "still in the OpenCode inbox" in await rpc("correctAnnotationDelivery", expect_error=True, deliveryId=key)
                assert len(posted) == 1
                recent = (await agent("annotate", "deliveries", "latest"))["deliveries"]
                assert recent == [{field: value for field, value in receipt.items() if field != "delivery"}]
                assert (await rpc("annotationDeliveries", id=annotation))["deliveries"] == recent
                database = Path(env["RUMBLE_DATA_DIR"]) / ".annotation-deliveries.sqlite3"
                assert database.stat().st_mode & 0o777 == 0o600
                assert b"history books" not in database.read_bytes()
                endpoint, prompt = posted[0]
                assert endpoint == "/api/session/ses_project/prompt" and prompt["delivery"] == "queue"
                assert prompt["text"] == handoff["markdown"]
                assert prompt["files"][0]["uri"] == "data:image/png;base64," + base64.b64encode(sheet.read_bytes()).decode()
                assert "refused" in await rpc("sendAnnotation", expect_error=True, id=annotation,
                                              target="ses_redirect", service=service)
                assert [p for p, _ in posted] == [endpoint, "/api/session/ses_redirect/prompt"]
                outcome["status"] = 503
                assert "did not confirm delivery" in await rpc("sendAnnotation", expect_error=True, id=annotation,
                                                                target="ses_project", service=service)
                assert len(posted) == 3
                assert (await rpc("annotationDeliveries", id=annotation))["deliveries"][0]["status"] == "unconfirmed"
                assert (await agent("annotate", "show", "latest")) == detail
                registration.write_text(json.dumps({**desc, "version": "another-service"}))
                assert "no longer available" in await rpc("undoAnnotationDelivery", expect_error=True, deliveryId=key)
                assert not any(method == "DELETE" for method, _ in requests)
                registration.write_text(json.dumps(desc))
                undone = await agent("annotate", "undo", key)
                assert undone["status"] == "withdrawn" and not undone["canUndo"]
                assert outcome["inbox"] == ["msg_other"]
                assert [(method, path) for method, path in requests if method == "DELETE"] == [
                    ("DELETE", "/api/session/ses_project/inbox/msg_fixture")]
                count = len(requests)
                assert await rpc("undoAnnotationDelivery", deliveryId=key) == undone and len(requests) == count

                outcome["status"] = 200
                raced = await rpc("sendAnnotation", id=annotation, target="ses_project", service=service)
                outcome["race"] = True
                result = await rpc("undoAnnotationDelivery", deliveryId=raced["deliveryId"])
                assert result["status"] == "received" and not result["canUndo"]
                assert len(posted) == 4  # Undo observes delivery before the user opts to send a correction.
                outcome["race"] = False
                outcome["staged_revert"] = True
                assert "staged revert" in await rpc("correctAnnotationDelivery", expect_error=True, deliveryId=raced["deliveryId"])
                assert len(posted) == 4
                outcome["staged_revert"] = False
                corrected = await agent("annotate", "correct", raced["deliveryId"])
                assert corrected["status"] == "corrected" and not corrected["canCorrect"]
                assert corrected["message"] == raced["message"] and corrected["correctionMessage"] == "msg_fixture_5"
                assert posted[4][0] == endpoint
                assert posted[4][1] == {
                    "text": f"Please disregard the Jard screen annotation in message `{raced['message']}` "
                            f"(capture `{annotation}`). I sent that annotation by mistake. "
                            "This correction applies only to that annotation.",
                    "delivery": "steer",
                }
                count = len(requests)
                assert await rpc("correctAnnotationDelivery", deliveryId=raced["deliveryId"]) == corrected
                assert len(requests) == count
                assert outcome["inbox"] == ["msg_other", "msg_fixture_5"]

                late = await rpc("sendAnnotation", id=annotation, target="ses_project", service=service)
                outcome["race"] = True
                assert (await agent("annotate", "undo", late["deliveryId"]))["status"] == "received"
                outcome["race"] = False
                outcome["status"] = 503
                uncertain = await rpc("correctAnnotationDelivery", deliveryId=late["deliveryId"])
                assert uncertain["status"] == "correction-unconfirmed" and not uncertain["canCorrect"]
                count = len(posted)
                assert (await agent("annotate", "correct", late["deliveryId"])) == uncertain
                assert len(posted) == count
                assert set(path for method, path in requests if method == "POST") == {
                    endpoint, "/api/session/ses_redirect/prompt"}
                outcome["status"] = 200

                unconfirmed = await rpc("sendAnnotation", id=annotation, target="ses_project", service=service)
                outcome["lose_confirmation"] = True
                result = await rpc("undoAnnotationDelivery", deliveryId=unconfirmed["deliveryId"])
                assert result["status"] == "withdrawal-unconfirmed"
                count = sum(method == "DELETE" for method, _ in requests)
                outcome["unreadable"] = False
                assert (await rpc("undoAnnotationDelivery", deliveryId=result["deliveryId"])) == result
                assert sum(method == "DELETE" for method, _ in requests) == count
                assert (await agent("annotate", "show", "latest")) == detail
                if not phone_fixture:
                    outcome["lose_confirmation"] = False
                    rule = await agent("annotate", "remember", annotation, "--scope", "site", "--to", "ses_project",
                                       "--service", service, "--automatic")
                    assert rule["automatic"]
                    await rpc("annotationLease", id=annotation, active=False)

                    async def new_capture(origin=detail["origin"], *, automatic=True):
                        capture = str(uuid4())
                        assert (await rpc("annotationLease", id=capture, active=True))["active"]
                        native_folder = root / ("jard-annotation-" + capture)
                        native_folder.mkdir(mode=0o700)
                        (native_folder / movie.name).write_bytes(saved_movie)
                        for asset in (manifest_file, sheet):
                            (native_folder / asset.name).write_bytes(asset.read_bytes())
                        native_manifest = native_folder / manifest_file.name
                        native_manifest.write_text(json.dumps({**json.loads(native_manifest.read_text()), "origin": origin}))
                        staging = await rpc("importAnnotation", id=capture, path=str(native_folder / movie.name), assets=True,
                                            languages=["en"], routeOrigin=origin)
                        assert staging["id"] == capture
                        if automatic:
                            assert staging["routing"]["capture"] == capture and staging["routing"]["phase"] == "transcribing"
                        else:
                            assert "routing" not in staging
                        await rpc("annotationLease", id=capture, active=False)
                        return capture

                    async def route_phase(capture, phase):
                        def matches(row):
                            data = row.get("detail", {})
                            return row.get("event") == "jard-annotation-routing" and data.get("capture") == capture and data.get("phase") == phase
                        while not any(matches(row) for row in seen):
                            await event()

                    async def route_confirmation(capture):
                        while True:
                            for row in seen:
                                if row.get("event") == "jard-annotation-confirmation" and row["detail"]["capture"] == capture:
                                    return row["detail"]["nonce"]
                            await event()

                    manual = len(posted)
                    cancelled = await new_capture()
                    await route_phase(cancelled, "offer")
                    assert len(posted) == manual
                    await rpc("annotationRoutePanel", capture=cancelled, action="shown", held=True)
                    status = await agent("annotate", "route", cancelled)
                    assert status["phase"] == "held" and len(posted) == manual
                    assert "automatic delivery is pending" in await rpc("annotationPanelSend", id=cancelled, target="ses_project", service=service, expect_error=True)
                    assert "Automatic delivery is pending" in await rpc("annotationHandoff", id=cancelled, waitForNarration=True, expect_error=True)
                    await rpc("annotationRoutePanel", capture=cancelled, action="resume")
                    nonce = await route_confirmation(cancelled)
                    assert (await rpc("annotationRouteConfirm", capture=cancelled, nonce=nonce, state="unavailable")) == {"accepted": True}
                    await route_phase(cancelled, "cancelled")
                    assert len(posted) == manual
                    assert (await rpc("annotationRoute", id=cancelled, action="cancel"))["phase"] == "cancelled"
                    assert "could not be found" in await rpc("annotationRouteUndo", id=cancelled, expect_error=True)
                    assert "history books" in (await rpc("annotationHandoff", id=cancelled, waitForNarration=True))["markdown"].lower()

                    registration.chmod(0o644)
                    capture = await new_capture()
                    await route_phase(capture, "waiting")
                    assert len(posted) == manual
                    assert (await rpc("desktopQuiesce"))["ready"]
                    paused = await agent("annotate", "route", capture)
                    assert paused["phase"] == "waiting"
                    assert "Automatic delivery is pending" in await rpc("annotationHandoff", id=capture, waitForNarration=True, expect_error=True)
                    registration.chmod(0o600)
                    assert (await rpc("updateAbort"))["ready"]
                    await route_phase(capture, "offer")
                    assert len(posted) == manual
                    await rpc("annotationRoutePanel", capture=capture, action="shown", held=True)
                    await rpc("annotationRoutePanel", capture=capture, action="resume")
                    nonce = await route_confirmation(capture)
                    assert (await rpc("annotationRouteConfirm", capture=capture, nonce=str(uuid4()), state="ready")) == {"accepted": False}
                    assert len(posted) == manual
                    assert (await rpc("annotationRouteConfirm", capture=capture, nonce=nonce, state="ready")) == {"accepted": True}
                    await route_phase(capture, "sent")
                    assert len(posted) == manual + 1
                    assert capture in posted[-1][1]["text"] and "history books" in posted[-1][1]["text"].lower()
                    assert "already been attempted" in await rpc("annotationHandoff", id=capture, waitForNarration=True, expect_error=True)
                    assert (await rpc("annotationReview", id=capture))["imagePath"]
                    receipt = (await agent("annotate", "deliveries", capture))["deliveries"][0]
                    assert receipt["status"] == "queued"
                    route_status = await rpc("annotationRoute", id=capture)
                    assert route_status["receiptStatus"] == "queued" and route_status["undoUntil"] == receipt["undoUntil"]
                    assert any(row.get("detail", {}).get("capture") == capture and row["detail"].get("undoUntil") == receipt["undoUntil"]
                               for row in seen if row.get("event") == "jard-annotation-routing")
                    messages = list(outcome["inbox"])
                    before_undo = len(requests)
                    undone = await rpc("annotationRouteUndo", id=capture)
                    assert undone["receiptStatus"] == "withdrawn" and "undoUntil" not in undone
                    assert receipt["message"] in messages
                    messages.remove(receipt["message"])
                    assert outcome["inbox"] == messages
                    assert await rpc("annotationRouteUndo", id=capture) == undone
                    assert [row for row in requests[before_undo:] if row[0] == "DELETE"] == [
                        ("DELETE", "/api/session/ses_project/inbox/" + receipt["message"])]
                    assert (await agent("annotate", "deliveries", capture))["deliveries"][0]["status"] == "withdrawn"
                    assert (await agent("annotate", "forget", annotation, "--rule", rule["id"]))["forgotten"]
                    (await web.delete(f"/api/sessions/{cancelled}")).raise_for_status()
                    (await web.delete(f"/api/sessions/{capture}")).raise_for_status()
                    print("Automatic routing sent once after recovery, and panel Undo withdrew only the matching inbox item without reposting.", flush=True)
                    terminal_origin = {"app": "com.mitchellh.ghostty", "name": "Ghostty", "terminal": {
                        "folder": "/fixture/Jard/src", "project": "/fixture/Jard",
                        "title": "Release review", "session": "ses_later",
                    }}
                    terminal_capture = await new_capture(terminal_origin, automatic=False)
                    await completed(terminal_capture)
                    terminal_destinations = await agent("annotate", "destinations", "--capture", terminal_capture)
                    assert terminal_destinations["scopes"] == [{"kind": "terminal", "key": "/fixture/Jard", "label": "Jard"}]
                    assert terminal_destinations["suggestion"] == {"sessionId": "ses_project", "reason": "Open in the captured terminal"}
                    assert "does not have" in await rpc("rememberAnnotationDestination", expect_error=True,
                        id=terminal_capture, scope="app", target="ses_project", service=service)
                    terminal_rule = await agent("annotate", "remember", terminal_capture, "--scope", "terminal",
                                                "--to", "ses_project", "--service", service)
                    assert terminal_rule["scope"]["key"] == "/fixture/Jard"
                    assert (await agent("annotate", "forget", terminal_capture, "--rule", terminal_rule["id"]))["forgotten"]
                    assert (await agent("annotate", "route", terminal_capture))["phase"] == "idle"
                    assert len(posted) == manual + 1

                    listed = await rpc("annotationDestinations", id=terminal_capture, query="Release")
                    assert listed["service"] == service and [row["id"] for row in listed["sessions"]] == ["ses_project"]
                    prior = await agent("annotate", "remember", terminal_capture, "--scope", "terminal", "--to", "ses_later", "--service", service)
                    assert prior["target"] == "ses_later" and not prior["automatic"]
                    assert "Refresh" in await rpc("annotationPanelSend", id=terminal_capture, target="ses_project", expect_error=True)
                    assert "does not have that captured" in await rpc("annotationPanelSend", id=terminal_capture,
                        target="ses_project", service=service, rememberScope="app", expect_error=True)
                    result = await rpc("annotationPanelSend", id=terminal_capture, target="ses_project", service=listed["service"], rememberScope="terminal")
                    assert result["phase"] == "sent" and result["receiptStatus"] == "queued" and result["undoUntil"] > time.time() * 1000
                    assert result["rememberedRule"] == {"status": "saved", "label": "Jard"}
                    installed_rule = (await rpc("annotationDestinations", id=terminal_capture))["rule"]
                    assert installed_rule["target"] == "ses_project" and installed_rule["automatic"]
                    assert terminal_capture in posted[-1][1]["text"] and len(posted) == manual + 2
                    next_capture = await new_capture(terminal_origin)
                    await route_phase(next_capture, "offer")
                    assert (await rpc("annotationRoute", id=next_capture, action="cancel"))["phase"] == "cancelled"
                    assert len(posted) == manual + 2
                    (await web.delete(f"/api/sessions/{next_capture}")).raise_for_status()
                    assert "already attempted" in await rpc("annotationPanelSend", id=terminal_capture, target="ses_project", service=service, expect_error=True)
                    assert "does not belong" in await rpc("annotationPanelUndo", id=cancelled, deliveryId=result["deliveryId"], expect_error=True)
                    back = await rpc("annotationPanelUndo", id=terminal_capture, deliveryId=result["deliveryId"])
                    assert back["receiptStatus"] == "withdrawn" and "undoUntil" not in back
                    assert back["rememberedRule"]["status"] == "restored"
                    restored_rule = (await rpc("annotationDestinations", id=terminal_capture))["rule"]
                    assert restored_rule["target"] == prior["target"] and restored_rule["automatic"] == prior["automatic"]
                    assert restored_rule["id"] != prior["id"]
                    assert any(row.get("event") == "jard-annotation-delivery-changed" and row["detail"]["capture"] == terminal_capture for row in seen)
                    outcome["status"] = 503
                    uncertain = await rpc("annotationPanelSend", id=terminal_capture, target="ses_project", service=service, rememberScope="terminal")
                    assert uncertain["phase"] == "unconfirmed" and len(posted) == manual + 3
                    assert (await rpc("annotationDestinations", id=terminal_capture))["rule"]["id"] == restored_rule["id"]
                    assert "already attempted" in await rpc("annotationPanelSend", id=terminal_capture, target="ses_later", service=service, expect_error=True)
                    assert len(posted) == manual + 3
                    outcome["status"] = 200
                    (await web.delete(f"/api/sessions/{terminal_capture}")).raise_for_status()
                    print("Terminal provenance resolved the visible OpenCode conversation and scoped rules to its project.", flush=True)
                    print("The preview sent and remembered the exact OpenCode destination, restored the prior rule on Undo, and kept it after an uncertain send.", flush=True)
            print("OpenCode search, receipts, exact-item withdrawal, targeted corrections, races, and unknown outcomes passed through the workspace and CLI.", flush=True)
            (await web.delete(f"/api/sessions/{picked['id']}")).raise_for_status()
            assert (await web.get(assets["manifestUrl"])).status_code == (await web.get(assets["contactSheetUrl"])).status_code == 404
            assert not (await agent("annotate", "list"))["annotations"]
            assert not (await rpc("annotationLease", id=annotation, active=False))["active"]
            print("Screen annotation movie, persistent strokes, contact sheet, native exports, and deletion passed.", flush=True)
            for mode in ("silent", "mac", "both"):
                capture = str(uuid4())
                folder = root / ("jard-annotation-" + capture)
                folder.mkdir(mode=0o700)
                movie = folder / "Screen annotation.mp4"
                sources = ["-f", "lavfi", "-i", "color=gray:s=160x90:r=12"]
                if mode != "silent":
                    audio_input = "sine=frequency=997:sample_rate=48000" if mode == "both" else str(audio)
                    sources += (["-f", "lavfi"] if mode == "both" else []) + ["-i", audio_input]
                    if mode == "both":
                        sources += ["-af", "volume=0.12"]
                subprocess.run(["ffmpeg", "-v", "error", *sources, "-t", str(seconds),
                                "-c:v", "mpeg4", "-c:a", "aac", movie], check=True)
                (folder / "annotation.json").write_bytes(manifest_bytes)
                (folder / "contact-sheet.png").write_bytes(sheet.read_bytes())
                assert (await rpc("annotationLease", id=capture, active=True))["active"]
                inputs = {}
                if mode == "both":
                    narration = folder / "Narration.wav"
                    await rpc("annotationAudio", id=capture, action="prepare", path=str(narration), inputDevice="FixtureWarmReady")
                    await rpc("annotationAudio", id=capture, action="start", at=time.monotonic())
                    await rpc("annotationAudio", id=capture, action="stop", at=time.monotonic())
                    inputs["narration"] = str(narration)
                assert (await rpc("importAnnotation", id=capture, path=str(movie), assets=True, languages=["en"], **inputs))["id"] == capture
                await rpc("annotationLease", id=capture, active=False)
                saved = await completed(capture)
                assert abs(saved["transcript"]["durationSeconds"] - seconds) < .2, saved["transcript"]
                exported = (await web.get(saved["audioUrl"])).raise_for_status().content
                assert (await web.get(saved["annotation"]["contactSheetUrl"])).raise_for_status().content == sheet.read_bytes()
                if mode == "silent":
                    assert not saved["transcript"]["text"] and exported == movie.read_bytes()
                else:
                    assert "history books" in saved["transcript"]["text"].lower()
                    if mode == "mac":
                        assert exported == movie.read_bytes()
                    else:
                        final_movie = root / "annotation-audio-mix.mp4"
                        final_movie.write_bytes(exported)
                        decoded = subprocess.check_output(["ffmpeg", "-v", "error", "-i", final_movie,
                            "-f", "f32le", "-ar", "48000", "-ac", "1", "-"])
                        mixed = np.frombuffer(decoded, "<f4")
                        tail = mixed[-24_000:]
                        peak = np.argmax(abs(np.fft.rfft(tail * np.hanning(len(tail)))))
                        assert abs(peak * 2 - 997) < 5 and np.max(np.abs(tail)) > .002
                (await web.delete(f"/api/sessions/{capture}")).raise_for_status()
            print("Annotations saved video-only, Mac audio only, and both audio sources, with local speech and a verified mix.", flush=True)
            seen.clear()
            forced_meeting = str(uuid4())
            await rpc("record", mode="both", meeting=forced_meeting, meetingLabel="Teams", languages=["en"])
            await wait_event("started")
            assert "Finish your recording" in await rpc("desktopQuiesce", expect_error=True)
            assert (await rpc("desktopQuiesce", force=True))["ready"]
            accepted = await wait_event("accepted")
            assert accepted["meeting"] == forced_meeting
            preserved = await completed(accepted["id"])
            assert preserved["meeting"] and preserved["audioUrl"] and set(preserved["recordingTracks"]) == {"mic", "system"}, preserved
            assert "preparing to restart" in await rpc("record", mode="both", expect_error=True)
            assert (await rpc("updateAbort"))["ready"]
            print("Forced close finalized a running meeting and preserved both tracks before the desktop could exit.", flush=True)
            events = [json.loads(row) for row in (root / "logs" / "live.ndjson").read_text().splitlines()]
            assert any(row["kind"] == "meeting" and row["phase"] == "accepted" for row in events)
            assert any(row["phase"] == "inference_complete" and row.get("inferenceMs", 0) > 0 for row in events)
            envelope = {"time", "kind", "phase"}
            transformation_fields = {
                "console": {"level", "message"},
                "network": {"host", "status", "durationMs", "outcome", "requestBytes", "responseBytes"},
                "program": {"program", "exitCode", "timedOut", "durationMs", "outcome", "inputBytes", "stdoutBytes", "stderrBytes"},
                "finished": {"step", "durationMs"},
                "failed": {"step", "durationMs"},
            }
            for row in events:
                fields = transformation_fields[row["phase"]] if row["kind"] == "transformation" else {"inferenceMs"}
                if row["phase"] == "timing":
                    fields = {"inferenceMs", "outputWaitMs", "totalMs"}
                assert set(row) <= envelope | fields, row
                if row["kind"] == "transformation" and row["phase"] == "console":
                    assert row["message"] == "Fixture transformation finished", row
            outside = str(uuid4())
            assert (await web.post("/api/clients/" + outside)).status_code == 204
            assert "Close other Jard" in await rpc("updateQuiesce", expect_error=True)
            assert (await web.delete("/api/clients/" + outside)).status_code == 204
            browser = "/api/clients/" + outside + "/stream"
            async with web.stream("GET", browser) as first_tab:
                first_lines = first_tab.aiter_lines()
                assert await anext(first_lines) == ": Jaro connected"
                async with web.stream("GET", browser) as second_tab:
                    second_lines = second_tab.aiter_lines()
                    assert await anext(second_lines) == ": Jaro connected"
                    assert "Close other Jard" in await rpc("updateQuiesce", expect_error=True)
                assert "Close other Jard" in await rpc("updateQuiesce", expect_error=True)
            # Disconnect without a DELETE, as when the browser closes without running pagehide.
            closed_at = time.monotonic()
            assert (await web.get("/api/status")).json()["status"]=="ready"
            async with asyncio.timeout(90):
                while True:
                    update = await rpc("updateQuiesce", expect_error=None)
                    if update["ok"]:
                        assert update["result"]["ready"]
                        break
                    message = update["error"]["message"]
                    if "Close other Jard" in message:
                        assert time.monotonic() - closed_at < 2, update
                    else:
                        assert "finish its current work" in message, update
                    await asyncio.sleep(.3)
            p.stdin.close()
            assert await p.wait()==0
            print("Update guards protected native and browser clients independently, then drained the engine after both streams closed.", flush=True)
        finally:
            if p.returncode is None:
                p.stdin.close()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(p.wait(),12)
                if p.returncode is None: p.kill(); await p.wait()
            with contextlib.suppress(httpx.RequestError):
                await web.post("/api/quit",headers={"Origin":base,"X-Jard-Action":"quit"})
            for _ in range(50):
                try: await web.get("/api/status",timeout=.3)
                except httpx.RequestError: break
                await asyncio.sleep(.2)


if __name__=="__main__":
    with tempfile.TemporaryDirectory(prefix="jard-bridge-test-") as work:
        asyncio.run(smoke(Path(work)))
