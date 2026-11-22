"""Run the exact recorder process control protocol with synthetic timestamped samples."""
import json
import os
import signal
import struct
import subprocess
import sys
import tempfile
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4


def exercise(executable, folder, mode):
    path = folder / (mode + ".wav")
    path.touch()
    tracks = {source: Path(f"{path}.{source}.wav") for source in ("mic", "system")} if mode == "stop" else {}
    for track in tracks.values():
        track.touch()
    options = ["--separate-tracks"] if tracks else []
    child = subprocess.Popen([executable, "record", "--mode", "both", "--output", str(path), *options],
                             env={**os.environ, "JARD_TEST_INPUT_LOSS": str(int(mode == "loss"))},
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        events = []
        request = str(uuid4()) if tracks else None
        for line in child.stdout:
            item = json.loads(line)
            events.append(item)
            if item["event"] == "levels" and item["seconds"] >= 2.25:
                if mode == "stop":
                    child.stdin.write(f"copy {request}\nstop\n")
                    child.stdin.flush()
                elif mode == "eof":
                    child.stdin.close()
                elif mode == "term":
                    child.send_signal(signal.SIGTERM)
                elif mode == "kill":
                    child.kill()
                break
        events.extend(map(json.loads, child.stdout))
        result = child.wait(timeout=5)
        assert events[0]["event"] == "started", events
        assert any(e["event"] == "levels" and e["system"] == .5 and e["mic"] == .25 for e in events)
        readings = [e["meetingRms"] for e in events if "meetingRms" in e]
        if tracks:
            assert 0.5 in readings, events
        else:
            assert not readings, events
        with wave.open(str(path), "rb") as w:
            assert w.getframerate() == 48000 and w.getnchannels() == 1 and w.getsampwidth() == 2
            frames = w.getnframes()
            assert frames >= 48000 and path.stat().st_size >= 44 + frames * 2
            w.setpos(16000)
            assert abs(struct.unpack("<h", w.readframes(1))[0] - 12288) < 3
        if mode == "kill":
            assert result == -signal.SIGKILL
            assert path.stat().st_size - 44 - frames * 2 <= 2 * 48000
        else:
            assert result == 0 and events[-1]["event"] == "finished", events[-1]
            assert path.stat().st_size == frames * 2 + 44
            if mode == "loss":
                assert "timing reference" in events[-1]["reason"], events[-1]
        for source, track in tracks.items():
            with wave.open(str(track), "rb") as w:
                assert w.getnframes() == frames
                assert w.getframerate() == 48000 and w.getnchannels() == 1
                w.setpos(16000)
                expected = 8192 if source == "mic" else 16384
                assert abs(struct.unpack("<h", w.readframes(1))[0] - expected) < 3
            assert track.stat().st_size == path.stat().st_size
        if request:
            cutoff = next(event for event in events if event["event"] == "meetingCopy")
            assert cutoff["request"] == request and 2 * 48000 <= cutoff["frame"] <= frames, cutoff
        return f"{mode}: {frames} valid mixed samples"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def exercise_microphone_only(executable, folder):
    path = folder / "mic-only.wav"
    path.touch()
    with subprocess.Popen([executable, "record", "--mode", "mic", "--output", str(path)],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as child:
        try:
            started = json.loads(child.stdout.readline())
            assert started["event"] == "started" and started["mode"] == "mic", started
            for line in child.stdout:
                event = json.loads(line)
                if event["event"] == "levels":
                    assert event["system"] == 0 and "meetingRms" not in event, event
                    if event["seconds"] >= 1.25:
                        assert event["mic"] == .25, event
                        break
            else:
                raise AssertionError("Microphone recording ended before producing levels.")
            remaining, _ = child.communicate("stop\n", timeout=5)
            assert child.returncode == 0 and json.loads(remaining.splitlines()[-1])["event"] == "finished", remaining
        finally:
            if child.poll() is None:
                child.kill()
    with wave.open(str(path)) as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (48000, 1, 2)
        assert audio.getnframes() >= 48000
        audio.setpos(16000)
        assert struct.unpack("<h", audio.readframes(1)) == (8192,)
    assert list(folder.glob("mic-only*")) == [path]
    print("Mic-only capture preserved the microphone level and saved a single WAV.")


def exercise_overlapping_takes(executable, folder):
    meeting, dictation = folder / "meeting.wav", folder / "dictation.wav"
    tracks = [Path(f"{meeting}.{source}.wav") for source in ("mic", "system")]
    for path in (meeting, dictation, *tracks):
        path.touch()

    def launch(mode, path, flag):
        return subprocess.Popen([executable, "record", "--mode", mode, "--output", str(path), flag],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def levels(child, seconds):
        for line in child.stdout:
            event = json.loads(line)
            if event["event"] == "levels" and event["seconds"] >= seconds:
                return event
        raise AssertionError("Recorder exited before producing its expected levels.")

    with launch("both", meeting, "--separate-tracks") as call:
        try:
            before = levels(call, 1)["seconds"]
            with launch("mic", dictation, "--meter") as take:
                try:
                    levels(take, 1.25)
                    take.stdin.write("finish\n")
                    take.stdin.flush()
                    assert take.wait(timeout=3) == 0
                    assert json.loads(take.stdout.readlines()[-1])["event"] == "finished"
                finally:
                    if take.poll() is None:
                        take.kill()
            levels(call, before + 1.5)
            call.stdin.write("stop\n")
            call.stdin.flush()
            assert call.wait(timeout=3) == 0
        finally:
            if call.poll() is None:
                call.kill()

    with wave.open(str(meeting), "rb") as mixed, wave.open(str(dictation), "rb") as mic_only:
        frames = mixed.getnframes()
        assert frames >= mic_only.getnframes() + 48000
        mic_only.setpos(24000)
        assert struct.unpack("<h", mic_only.readframes(1))[0] == 8192
    for path, expected in zip(tracks, (8192, 16384)):
        with wave.open(str(path), "rb") as track:
            assert track.getnframes() == frames
            track.setpos(24000)
            samples = track.readframes(frames - 48000)
            assert samples == struct.pack("<h", expected) * (len(samples) // 2)
    print("Overlapping recorder processes preserved both meeting tracks and the shorter microphone-only take.")

def exercise_source_handoff(executable, root, *, system_restart):
    fixture = "system-restart" if system_restart else "mic-handoff"
    recording = root / (fixture + ".wav")
    microphone = Path(str(recording) + ".mic.wav")
    system = Path(str(recording) + ".system.wav")
    for path in (recording, microphone, system):
        path.touch()
    with subprocess.Popen(
        [executable, "record", "--mode", "both", "--output", str(recording), "--separate-tracks"],
        env={**os.environ, ("JARD_TEST_SYSTEM_RESTART" if system_restart else "JARD_TEST_MIC_HANDOFF"): "1"},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    ) as child:
        try:
            for line in child.stdout:
                event = json.loads(line)
                if event.get("seconds", 0) >= 1:
                    child.stdin.write("stop\n")
                    child.stdin.flush()
                    break
            assert child.wait(timeout=3) == 0
        finally:
            if child.poll() is None:
                child.kill()
    with wave.open(str(microphone), "rb") as mic, wave.open(str(system), "rb") as mac:
        assert mic.getnframes() == mac.getnframes()
        signal = struct.unpack(f"<{mic.getnframes()}h", mic.readframes(mic.getnframes()))
        first = signal.index(8192)
        handoff = first + 20 * 960
        mac.setpos(first)
        if system_restart:
            assert signal[first:first + 32000] == (8192,) * 32000
            assert mac.readframes(19200) == struct.pack("<h", 16384) * 19200
            assert mac.readframes(4800) == b"\0\0" * 4800
            assert mac.readframes(9000) == struct.pack("<h", 12288) * 9000
            print("The Mac audio restart retained the gap, recovered on the same clock, and left the microphone continuous.")
        else:
            assert signal[first:handoff] == (8192,) * (20 * 960)
            assert signal[handoff:handoff + 12000] == (4096,) * 12000
            assert mac.readframes(32000) == struct.pack("<h", 16384) * 32000
            print("Meeting microphone handoff dropped old tap callbacks and overlap while Mac audio stayed continuous.")


def exercise_system_fallback(executable, root, *, scenario="waiting"):
    recording = root / f"system-fallback-{scenario}.wav"
    microphone = Path(str(recording) + ".mic.wav")
    system = Path(str(recording) + ".system.wav")
    for path in (recording, microphone, system):
        path.touch()
    backend = "tap" if scenario == "recover" else "screen"
    option = "tap" if backend == "tap" else "tapAndScreen"
    stop_after = 2.75 if scenario == "recover" else 1.25
    with subprocess.Popen(
        [executable, "record", "--mode", "both", "--output", str(recording), "--separate-tracks",
         "--system-audio", option, "--host-pid", str(os.getpid())],
        env={**os.environ, "JARD_TEST_SYSTEM_FALLBACK": scenario},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    ) as child:
        statuses = []
        late_closed = 0
        try:
            for line in child.stdout:
                event = json.loads(line)
                if event["event"] == "meetingSystemAudio":
                    statuses.append((event["available"], event.get("backend")))
                elif event["event"] == "fixtureLateTapClosed":
                    late_closed += 1
                elif event.get("seconds", 0) >= stop_after:
                    child.stdin.write("stop\n")
                    child.stdin.flush()
                    break
            assert child.wait(timeout=3) == 0
        finally:
            if child.poll() is None:
                child.kill()
    assert statuses == [(False, None), (True, backend)], statuses
    assert late_closed == int(scenario != "waiting")
    with wave.open(str(microphone), "rb") as mic, wave.open(str(system), "rb") as mac:
        count = mic.getnframes()
        assert count == mac.getnframes()
        mic_signal = struct.unpack(f"<{count}h", mic.readframes(count))
        mac_signal = struct.unpack(f"<{count}h", mac.readframes(count))
        first_mic = mic_signal.index(8192)
        first_mac = mac_signal.index(16384)
        assert first_mac - first_mic >= 16_000
        if scenario == "recover":
            assert 70_000 <= first_mac - first_mic <= 110_000
        assert not any(mac_signal[:first_mac])
        assert set(mac_signal) <= {0, 16384}, "An abandoned tap leaked audio into the fallback track."
        assert mic_signal[first_mic:first_mac + 8000] == (8192,) * (first_mac + 8000 - first_mic)
        assert mac_signal[first_mac:first_mac + 8000] == (16384,) * 8000
    if scenario == "recover":
        print("A recovered tap restarted before backoff expired and joined the ongoing microphone track.")
    elif scenario == "open":
        print("A stalled open timed out, started fallback, and closed the late tap without mixing its audio.")
    else:
        print("A waiting tap never reported ready. ScreenCaptureKit fallback began on the same clock while the mic kept recording.")


def exercise_timed_tails(executable, root):
    def wait_for(child, kind, seconds=0):
        for line in child.stdout:
            event = json.loads(line)
            if event["event"] == kind and event.get("seconds", 0) >= seconds:
                return event
        raise AssertionError(f"Recorder exited before {kind}.")

    for warm, speech_end, expected in ((False, 24, 0.2), (True, 36, 0.47), (False, 80, 0.6), (True, 80, 0.6)):
        path = root / f"tail-{warm}-{speech_end}.wav"
        path.touch()
        args = ["warm"] if warm else ["record", "--mode", "mic", "--output", str(path), "--meter"]
        with subprocess.Popen(
            [executable, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            env={**os.environ, "JARD_TEST_SPEECH_FRAMES": str(speech_end)},
        ) as child:
            try:
                if warm:
                    wait_for(child, "warmReady")
                    child.stdin.write(json.dumps({"start": str(path), "preRoll": False}) + "\n")
                    child.stdin.flush()
                wait_for(child, "levels", seconds=0.4)
                # The host shortcut clock is mach_absolute_time, also used by Python's perf_counter here.
                release = time.perf_counter()
                time.sleep(0.09)  # Relay delay must not move the release point in the WAV.
                child.stdin.write(f"finish {release:.9f}\n")
                child.stdin.flush()
                finish = wait_for(child, "finished")
                with wave.open(str(path), "rb") as stream:
                    before_release = finish["releaseFrame"] / stream.getframerate()
                    tail = stream.getnframes() / stream.getframerate() - before_release
                    assert 0.39 <= before_release <= 0.53, finish
                    assert abs(tail - expected) < (0.065 if expected == 0.47 else 0.0001), finish
                if warm:
                    wait_for(child, "takeEnded")
                    child.stdin.close()
                assert child.wait(timeout=3) == 0
            finally:
                if child.poll() is None:
                    child.kill()
    print("Timed cold and warm WAVs keep the short ending, grow into a pause, and enforce the full tail cap.")

    path = root / "tail-before-start.wav"
    path.touch()
    release = time.perf_counter()
    with subprocess.Popen(
        [executable, "record", "--mode", "mic", "--output", str(path), "--meter"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        env={**os.environ, "JARD_TEST_START_DELAY": "1"},
    ) as child:
        try:
            child.stdin.write(f"finish {release:.9f}\n")
            child.stdin.flush()
            assert child.wait(timeout=3) == 0
            finish = wait_for(child, "finished")
            assert finish["releaseFrame"] == 0, finish
            with wave.open(str(path), "rb") as stream:
                assert stream.getnframes() == 0, finish
        finally:
            if child.poll() is None:
                child.kill()

def exercise_microphone_interruption(executable, root):
    cases = ((False, "loss"), (False, "gap"), (True, "stall"), (True, "preRoll"), (True, "zero"))
    for warm, mode in cases:
        path = root / f"interruption-{mode}.wav"
        path.touch()
        args = ["warm"] if warm else ["record", "--mode", "mic", "--output", str(path), "--meter"]
        with subprocess.Popen(
            [executable, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            env={**os.environ, "JARD_TEST_INTERRUPTION": mode, **({"JARD_TEST_SPEECH_FRAMES": "8"} if mode == "zero" else {})},
        ) as child:
            try:
                if warm:
                    assert json.loads(child.stdout.readline())["event"] == "warmReady"
                    child.stdin.write(json.dumps({"start": str(path), "preRoll": mode == "preRoll"}) + "\n")
                    child.stdin.flush()
                if not warm:
                    assert child.wait(timeout=7) == 0
                if mode == "zero":
                    for line in child.stdout:
                        if json.loads(line).get("seconds", 0) >= .3:
                            break
                    child.stdin.write("stop\n")
                    child.stdin.flush()
                for line in child.stdout:
                    event = json.loads(line)
                    if event["event"] == "finished":
                        break
                else:
                    raise AssertionError("Missing interrupted WAV result.")
                with wave.open(str(path), "rb") as stream:
                    assert stream.getnframes() == round(event["seconds"] * 48000), event
                    if mode == "zero":
                        assert "interruption" not in event and event["reason"] == "Stopped", event
                        assert stream.getnframes() > 12_000 and not any(stream.readframes(stream.getnframes()))
                    else:
                        assert event["interruption"] == ("discontinuity" if mode == "gap" else "microphone"), event
                        assert event["hasLiveAudio"] is (mode != "preRoll") and event["capturedThrough"] > 0, event
                        expected = 960 if mode == "preRoll" else 19200
                        assert abs(stream.getnframes() - expected) < 480, event
                if warm:
                    if mode != "zero":
                        status = json.loads(child.stdout.readline())
                        assert status["event"] == "warmAvailability" and status["phase"] == "unavailable", status
                    reset = json.loads(child.stdout.readline())
                    assert reset == {"event": "fixtureWarmReset", "reason": "renegotiate" if mode == "zero" else "reopen"}, reset
                    assert json.loads(child.stdout.readline())["event"] == "takeEnded"
                    child.stdin.close()
                    assert child.wait(timeout=4) == 0
            finally:
                if child.poll() is None:
                    child.kill()
    print("Interruption WAVs ended at the last real packet, and the warm recorder reset after loss or digital silence.")

def exercise_abandoned_cold_open(executable, root):
    for action in ("stop", "finish"):
        path = root / f"abandoned-cold-{action}.wav"
        path.touch()
        arguments = ["record", "--mode", "mic", "--output", str(path), "--meter", "--cold-recovery",
                     "--input", "Fixture selected", "--avoid-bluetooth"]
        with subprocess.Popen(
            [executable, *arguments], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            env={**os.environ, "JARD_TEST_START_DELAY": "1"},
        ) as child:
            try:
                command = f"finish {time.perf_counter():.9f}" if action == "finish" else action
                child.stdin.write(command + "\n")
                child.stdin.flush()
                assert child.wait(timeout=4) == 0
                assert [json.loads(line)["event"] for line in child.stdout] == ["openingAbandoned"]
                assert path.stat().st_size == 0
            finally:
                if child.poll() is None:
                    child.kill()
    print("Stop and timestamped release abandoned a cold open before any WAV or ready event.")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(max_workers=5) as pool:
        for source in ("mic", "system"):
            path = Path(tmp) / (source + ".wav")
            path.touch()
            result = subprocess.run([sys.argv[1], "record", "--mode", source, "--output", str(path)],
                                    env={**os.environ, "JARD_TEST_DENIED_SOURCE": source}, input="", capture_output=True, text=True, timeout=5)
            event = json.loads(result.stdout)
            assert result.returncode == 1 and event["event"] == "error" and event["permission"] == source, event
            assert path.stat().st_size == 0
        print("Structured microphone and system audio denials left no recorded audio.")
        path = Path(tmp) / "open-failure.wav"
        path.touch()
        result = subprocess.run([sys.argv[1], "record", "--mode", "mic", "--output", str(path), "--meter"],
                                env={**os.environ, "JARD_TEST_OPEN_FAILURE": "1"}, input="", capture_output=True, text=True, timeout=5)
        failure = json.loads(result.stdout)
        assert result.returncode == 1 and failure["interruption"] == "microphone" and failure["failedAt"] > 0, failure
        assert path.stat().st_size == 0
        results = [pool.submit(exercise, sys.argv[1], Path(tmp), mode) for mode in ["stop", "eof", "term", "kill", "loss"]]
        for task in results:
            print(task.result(timeout=20))
        exercise_microphone_only(sys.argv[1], Path(tmp))
        exercise_overlapping_takes(sys.argv[1], Path(tmp))
        exercise_source_handoff(sys.argv[1], Path(tmp), system_restart=False)
        exercise_source_handoff(sys.argv[1], Path(tmp), system_restart=True)
        exercise_system_fallback(sys.argv[1], Path(tmp))
        exercise_system_fallback(sys.argv[1], Path(tmp), scenario="open")
        exercise_system_fallback(sys.argv[1], Path(tmp), scenario="recover")
        exercise_timed_tails(sys.argv[1], Path(tmp))
        exercise_microphone_interruption(sys.argv[1], Path(tmp))
        exercise_abandoned_cold_open(sys.argv[1], Path(tmp))
