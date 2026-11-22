"""Exercise two stages on one standby process using synthetic audio."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import wave
from uuid import uuid4


def run(binary):
    with tempfile.TemporaryDirectory(prefix="jard-warm-") as folder:
        root = Path(folder)
        helper = subprocess.Popen([binary, "warm"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            def wait_for(kind):
                while row := helper.stdout.readline():
                    event = json.loads(row)
                    assert event.get("event") != "error", event
                    if event.get("event") == kind:
                        return event
                raise AssertionError(f"The warm microphone exited before {kind}.")

            def send(command):
                helper.stdin.write(command + "\n")
                helper.stdin.flush()

            wait_for("warmReady")
            send('{"meter":1}')
            assert wait_for("warmMeter") == {"event": "warmMeter", "request": 1, "peak": None}
            time.sleep(.2)
            assert not list(root.iterdir()), "Standby wrote audio without a take."
            frames = []
            for index, command in enumerate(("stop", "stop", "finish")):
                path = root / f"{index}.wav"
                options = {"preRoll": False} if index else {}
                helper.stdin.write(json.dumps({"start": str(path), **options}) + "\n")
                helper.stdin.flush()
                wait_for("started")
                assert abs(wait_for("levels")["micRms"] - .25) < .00001
                if index == 0:
                    send('{"meter":2}')
                    time.sleep(.08)
                    send('{"meter":3}')
                    wait_for("warmMeter")
                    reading = wait_for("warmMeter")
                    assert reading["request"] == 3 and reading["peak"] is not None and abs(reading["peak"]) < .00001, reading
                time.sleep(.3)
                helper.stdin.write(command + "\n")
                helper.stdin.flush()
                wait_for("finished")
                wait_for("takeEnded")
                assert helper.poll() is None
                with wave.open(str(path), "rb") as wave_file:
                    assert wave_file.getframerate() == 48000
                    frames.append(wave_file.getnframes())
                    first_frames = wave_file.readframes(960)
                    samples = [int.from_bytes(first_frames[i:i + 2], "little", signed=True) for i in range(0, len(first_frames), 2)]
                    assert (min(samples) < 0) == (index == 0), (index, min(samples))
            assert min(frames) > 10_000 and frames[2] - frames[1] > 5000, frames

            first = root / "previous.wav"
            second = root / "queued.wav"
            send(json.dumps({"start": str(first), "preRoll": False}))
            wait_for("started")
            time.sleep(.56)
            pressed = time.perf_counter()
            send(f"finish {pressed - .04:.9f}")
            wait_for("finished")
            wait_for("takeEnded")
            send(json.dumps({"start": str(second), "queuedAt": pressed}))
            opened = wait_for("started")["openedAt"]
            assert opened - pressed > .09
            released = pressed + .34
            time.sleep(max(0, released - time.perf_counter()))
            send(f"finish {released:.9f}")
            finished = wait_for("finished")
            wait_for("takeEnded")
            expected_release_frame = (.45 + .34) * 48000
            assert abs(finished["releaseFrame"] - expected_release_frame) < 48, finished
            with wave.open(str(second), "rb") as wave_file:
                samples = wave_file.readframes(960)
                assert len(samples) == 1920 and samples == b"\x00\x20" * 960

            helper.stdin.close()
            assert helper.wait(timeout=5) == 0
            print("Warm capture retains pre-roll and release timing through a release-tail handoff.")
        finally:
            if helper.poll() is None:
                helper.kill()
                helper.wait()


def exercise_annotation_narration(binary):
    with tempfile.TemporaryDirectory(prefix="jaro-annotation-audio-") as folder:
        root = Path(folder)
        helper = subprocess.Popen([binary, "warm"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            def wait_for(kind):
                for line in helper.stdout:
                    event = json.loads(line)
                    assert event["event"] != "error" and "error" not in event, event
                    if event["event"] == kind:
                        return event
                raise AssertionError(f"Missing {kind}.")

            def send(message):
                helper.stdin.write((json.dumps(message) if isinstance(message, dict) else message) + "\n")
                helper.stdin.flush()

            wait_for("warmReady")
            receipt = root / "Narration.wav"
            start = time.perf_counter()
            send({"annotation": 1, "action": "start", "path": str(receipt), "at": start})
            assert wait_for("warmAnnotation")["request"] == 1
            time.sleep(.16)
            first = root / "first.wav"
            send({"start": str(first), "preRoll": False})
            wait_for("started")
            wait_for("levels")
            time.sleep(.2)
            send("stop")
            wait_for("finished")
            wait_for("takeEnded")
            assert receipt.exists()
            time.sleep(.16)
            second = root / "second.wav"
            send({"start": str(second), "preRoll": False})
            wait_for("started")
            wait_for("levels")
            time.sleep(.2)
            end = time.perf_counter()
            send({"annotation": 2, "action": "stop", "at": end})
            assert wait_for("warmAnnotation")["request"] == 2
            time.sleep(.1)
            send("stop")
            wait_for("finished")
            wait_for("takeEnded")
            assert helper.poll() is None and first.is_file() and second.is_file()

            with wave.open(str(receipt), "rb") as audio:
                frames = audio.getnframes()
                samples = audio.readframes(frames)
                assert frames == round((end - start) * 48000), (frames, end - start)
                assert not any(samples[: int(.08 * 48000) * 2])
                assert any(samples[int(.18 * 48000) * 2:])
            helper.stdin.close()
            assert helper.wait(timeout=5) == 0
            print("Annotation and dictation shared warm packets, preserved the initial gap, and stopped at independent boundaries.")
        finally:
            if helper.poll() is None:
                helper.kill()
                helper.wait()


def exercise_on_demand_annotation(binary):
    with tempfile.TemporaryDirectory(prefix="jaro-demand-annotation-") as folder:
        root = Path(folder)
        env = {**os.environ, "JARD_TEST_WARM_OPEN_FAILURES": "3"}
        helper = subprocess.Popen([binary, "warm", "--on-demand", "--input", "saved"], env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        history = []

        def send(message):
            helper.stdin.write((json.dumps(message) if isinstance(message, dict) else message) + "\n")
            helper.stdin.flush()

        def wait_for(kind):
            for line in helper.stdout:
                event = json.loads(line)
                history.append(event)
                assert event["event"] != "error" and "error" not in event, event
                if event["event"] == kind:
                    return event
            raise AssertionError(f"Missing {kind} from the on-demand worker.")

        try:
            wait_for("warmReady")
            time.sleep(.28)
            send({"meter": 1})
            wait_for("warmMeter")
            assert not any(event["event"] == "fixtureWarmOpen" for event in history)
            take, narration = root / "Take.wav", root / "Narration.wav"
            send({"start": str(take)})
            started = wait_for("started")
            attempts = [event for event in history if event["event"] == "fixtureWarmOpen"]
            assert [event["failing"] for event in attempts] == [True, True, True, False], attempts
            assert all(later["time"] - earlier["time"] >= .09 for earlier, later in zip(attempts[:2], attempts[1:3]))
            assert started["usingDefaultMicrophone"] and started["microphone"] == "Fixture system microphone", started
            wait_for("levels")
            start = time.perf_counter()
            send({"annotation": 1, "action": "start", "path": str(narration), "at": start})
            assert wait_for("warmAnnotation")["request"] == 1
            time.sleep(.32)
            send("stop")
            wait_for("finished")
            wait_for("takeEnded")
            end = time.perf_counter()
            send({"annotation": 2, "action": "stop", "at": end})
            assert wait_for("warmAnnotation")["request"] == 2
            with wave.open(str(narration), "rb") as file:
                frames = file.getnframes()
                assert frames == round((end - start) * 48000) and any(file.readframes(frames))
            with wave.open(str(take), "rb") as file:
                assert file.getnframes() > 10_000
            helper.stdin.close()
            assert helper.wait(timeout=5) == 0
            print("Mic-on-demand opened only for the take, retried the selected input, and let Annotate join the fallback.")
        finally:
            if helper.poll() is None:
                helper.kill()
                helper.wait()


def exercise_shared_meeting(binary):
    with tempfile.TemporaryDirectory(prefix="jaro-shared-meeting-") as folder:
        root = Path(folder)
        helper = subprocess.Popen([binary, "warm"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        history = []
        try:
            def wait_for(kind, meeting=None):
                for line in helper.stdout:
                    event = json.loads(line)
                    history.append(event)
                    assert event["event"] != "error" and "error" not in event, event
                    if event["event"] == kind and event.get("sharedMeeting") == meeting:
                        return event
                raise AssertionError(f"Missing {kind} for {meeting}.")

            def send(message):
                helper.stdin.write((json.dumps(message) if isinstance(message, dict) else message) + "\n")
                helper.stdin.flush()

            wait_for("warmReady")
            wait_for("warmAvailability")
            meeting = str(uuid4())
            path = root / "meeting"
            for suffix in (".mic.wav", ".system.wav"):
                (root / (path.name + suffix)).touch()
            narration = root / "Narration.wav"
            send({"annotation": 1, "action": "start", "path": str(narration), "at": time.perf_counter()})
            wait_for("warmAnnotation")
            send({"meeting": meeting, "start": str(path)})
            assert wait_for("started", meeting)["microphone"] == "Fixture system microphone"
            send({"start": str(root / "first.wav"), "preRoll": False})
            wait_for("started")
            request = str(uuid4())
            send({"meeting": meeting, "command": f"copy {request}"})
            assert wait_for("meetingCopy", meeting)["request"] == request
            send({"meeting": meeting, "command": 'input "fixture-selected"'})
            time.sleep(.4)
            send({"annotation": 2, "action": "stop", "at": time.perf_counter()})
            wait_for("warmAnnotation")
            assert not any(event["event"] == "sourceChanged" and event.get("sharedMeeting") == meeting for event in history)
            send("stop")
            wait_for("finished")
            wait_for("takeEnded")
            assert wait_for("sourceChanged", meeting)["microphone"] == "Fixture saved microphone"
            send({"start": str(root / "second.wav"), "preRoll": False})
            assert wait_for("started")["microphone"] == "Fixture saved microphone"
            second_started = len(history)
            time.sleep(.28)
            send({"meeting": meeting, "command": "stop"})
            wait_for("finished", meeting)
            wait_for("takeEnded", meeting)
            assert not any(event["event"] == "takeEnded" and not event.get("sharedMeeting") for event in history[second_started:])
            send("stop")
            wait_for("finished")
            wait_for("takeEnded")
            with wave.open(str(path), "rb") as mix, wave.open(str(path) + ".mic.wav", "rb") as mic, wave.open(
                str(path) + ".system.wav", "rb"
            ) as system:
                size = mix.getnframes()
                assert size == mic.getnframes() == system.getnframes() > 20_000
                assert b"\x00\x20" * 200 in mic.readframes(size)
                assert b"\x00\x40" * 200 in system.readframes(size)
            with wave.open(str(narration), "rb") as audio:
                assert b"\x00\x20" * 200 in audio.readframes(audio.getnframes())
            helper.stdin.close()
            assert helper.wait(timeout=5) == 0
            print("Annotation, meeting, and dictation shared a microphone. Meeting copy, route changes and stops stayed independent.")
        finally:
            if helper.poll() is None:
                helper.kill()
                helper.wait()


def exercise_standalone_meeting(binary):
    with tempfile.TemporaryDirectory(prefix="jaro-standalone-meeting-") as folder:
        root = Path(folder)
        call_input = root / "call-input"
        call_input.write_text("call-one")
        env = {**os.environ, "JARD_TEST_SHARED_MEETING_CALL_INPUT": str(call_input), "JARD_TEST_STALLED_MEETING_SYSTEM_AUDIO": "1"}
        helper = subprocess.Popen([binary, "warm", "--on-demand", "--input", "saved"], env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        events = []

        def send(message):
            helper.stdin.write((json.dumps(message) if isinstance(message, dict) else message) + "\n")
            helper.stdin.flush()

        def observe(kind, *, meeting=None):
            for line in helper.stdout:
                event = json.loads(line)
                events.append(event)
                assert event["event"] != "error" and "error" not in event, event
                if event["event"] == kind and event.get("sharedMeeting") == meeting:
                    return event
            raise AssertionError(f"Missing {kind} from the standalone meeting worker.")

        try:
            observe("warmReady")
            meeting = str(uuid4())
            path = root / "meeting"
            for suffix in (".mic.wav", ".system.wav"):
                path.with_suffix(suffix).touch()
            send({"meeting": meeting, "start": str(path), "inputDevice": "saved"})
            assert observe("started", meeting=meeting)["microphoneAvailable"] is False
            assert observe("fixtureWarmRouteOpen")["uid"] == "call-one"
            ready = observe("sourceChanged", meeting=meeting)
            assert ready["microphone"] == "Fixture call microphone" and ready["microphoneAvailable"]
            narration = root / "Narration.wav"
            send({"annotation": 1, "action": "start", "path": str(narration), "at": time.perf_counter()})
            observe("warmAnnotation")
            send({"start": str(root / "dictation.wav"), "preRoll": False})
            assert observe("started")["microphone"] == "Fixture call microphone"
            observe("levels")
            call_input.write_text("call-two")
            time.sleep(.3)
            send({"meter": 1})
            observe("warmMeter")
            assert not any(event.get("uid") == "call-two" for event in events)
            send("stop")
            observe("finished")
            observe("takeEnded")
            assert observe("fixtureWarmRouteOpen")["uid"] == "call-two"
            observe("sourceChanged", meeting=meeting)
            before_stop = time.monotonic()
            send({"meeting": meeting, "command": "stop"})
            observe("takeEnded", meeting=meeting)
            assert time.monotonic() - before_stop < 2, "Meeting stop waited for the stalled Mac audio input."
            send({"annotation": 2, "action": "stop", "at": time.perf_counter()})
            observe("warmAnnotation")
            with wave.open(str(narration), "rb") as audio:
                assert b"\x00\x20" * 200 in audio.readframes(audio.getnframes())
            with wave.open(str(path) + ".system.wav", "rb") as audio:
                assert b"\x00\x40" * 200 in audio.readframes(audio.getnframes())
            helper.stdin.close()
            assert helper.wait(timeout=5) == 0
            print("Standalone meeting preferred the call input, shared it with Annotate and dictation, and deferred a call route change until dictation stopped.")
        finally:
            if helper.poll() is None:
                helper.kill()
                helper.wait()


def exercise_zero_prefix(binary):
    with tempfile.TemporaryDirectory(prefix="jard-warm-zero-prefix-") as folder:
        root = Path(folder).resolve()
        env = {**os.environ, "JARD_TEST_FIRST_SPEECH_FRAME": "18", "JARD_TEST_SPEECH_FRAMES": "100"}
        helper = subprocess.Popen([binary, "warm"], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        events = []
        try:
            def observe(kind):
                while row := helper.stdout.readline():
                    event = json.loads(row)
                    events.append(event)
                    assert event["event"] != "error", event
                    if event["event"] == kind:
                        return event
                raise AssertionError(f"Missing {kind}.")

            def send(command):
                helper.stdin.write(command + "\n")
                helper.stdin.flush()

            observe("warmReady")
            first = root / "zero-prefix.wav"
            send(json.dumps({"start": str(first)}))
            opened = observe("started")["openedAt"]
            released = opened + .3
            time.sleep(max(0, released + .015 - time.perf_counter()))
            send(f"finish {released:.9f}")
            take = observe("finished")
            assert observe("fixtureWarmReset")["reason"] == "renegotiate"
            observe("takeEnded")
            with wave.open(str(first), "rb") as stream:
                samples = stream.readframes(stream.getnframes())
            assert any(samples[take["releaseFrame"] * 2:]), "Fixture did not contain spoken audio in the release tail."
            second = root / "cancelled.wav"
            send(json.dumps({"start": str(second), "preRoll": False}))
            observe("started")
            time.sleep(.25)
            send("cancel")
            assert observe("finished")["reason"] == "Cancelled"
            observe("takeEnded")
            helper.stdin.close()
            assert helper.wait(timeout=5) == 0
            assert [item["event"] for item in events].count("fixtureWarmReset") == 1
            assert [item["event"] for item in events].count("fixtureWarmClockReset") == 1
            print("Tail-only speech kept the zero-prefix clock reset armed once, and cancelling did not re-arm it.")
        finally:
            if helper.poll() is None:
                helper.kill()
                helper.wait()


def exercise_failed_open(binary):
    with tempfile.TemporaryDirectory(prefix="jaro-warm-reopen-") as folder:
        path = Path(folder).resolve() / "recovered.wav"
        env = {**os.environ, "JARD_TEST_WARM_OPEN_FAILURES": "2"}
        with subprocess.Popen([binary, "warm"], env=env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as helper:
            availability = []

            def wait_for(kind):
                while line := helper.stdout.readline():
                    event = json.loads(line)
                    assert event["event"] != "error", event
                    if event["event"] == "warmAvailability":
                        availability.append(event)
                    if event["event"] == kind:
                        return event
                raise AssertionError(f"Missing {kind} after a warm open failure.")

            try:
                wait_for("warmReady")
                first, second, third = [wait_for("fixtureWarmOpen") for _ in range(3)]
                assert [attempt["failing"] for attempt in (first, second, third)] == [True, True, False]
                assert second["time"] - first["time"] >= .25
                assert third["time"] - second["time"] >= .5
                assert wait_for("warmAvailability")["phase"] == "ready"
                assert [event["phase"] for event in availability] == ["unavailable", "ready"]
                assert "could not open" in availability[0]["message"]
                assert not path.exists()
                helper.stdin.write(json.dumps({"start": str(path), "preRoll": False}) + "\n")
                helper.stdin.flush()
                wait_for("started")
                wait_for("levels")
                time.sleep(.32)
                helper.stdin.write("stop\n")
                helper.stdin.flush()
                assert wait_for("finished")["seconds"] >= .3
                wait_for("takeEnded")
                helper.stdin.close()
                assert helper.wait(timeout=5) == 0
                with wave.open(str(path), "rb") as take:
                    assert any(take.readframes(take.getnframes()))
                print("The same warm worker recovered after failed startup opens and recorded a fresh take.")
            finally:
                if helper.poll() is None:
                    helper.kill()


def exercise_saved_input_fallback(binary):
    with tempfile.TemporaryDirectory(prefix="jaro-warm-fallback-") as folder:
        root = Path(folder)
        gate = root / "saved-input-available"
        env = {**os.environ, "JARD_TEST_WARM_SAVED_INPUT_GATE": str(gate)}
        with subprocess.Popen([binary, "warm", "--input", "fixture-saved"], env=env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as helper:
            events = []

            def wait_for(kind):
                while line := helper.stdout.readline():
                    event = json.loads(line)
                    events.append(event)
                    assert event["event"] != "error", event
                    if event["event"] == kind:
                        return event
                raise AssertionError(f"Missing {kind} during warm input fallback.")

            def send(command):
                helper.stdin.write(command + "\n")
                helper.stdin.flush()

            try:
                wait_for("warmReady")
                first = wait_for("fixtureWarmRouteOpen")
                fallback = wait_for("fixtureWarmRouteOpen")
                retry = wait_for("fixtureWarmRouteOpen")
                assert first["uid"] == retry["uid"] == "fixture-saved" and first["failing"] and retry["failing"]
                assert fallback["uid"] == "fixture" and not fallback["failing"]
                assert retry["time"] - first["time"] >= .25

                fallback_path = root / "fallback.wav"
                send(json.dumps({"start": str(fallback_path), "preRoll": False}))
                started = wait_for("started")
                assert started["microphone"] == "Fixture system microphone" and started["usingDefaultMicrophone"]
                before_switch = len(events)
                gate.touch()
                time.sleep(.75)
                send(f"finish {time.perf_counter():.9f}")
                assert wait_for("finished")["seconds"] >= .7
                wait_for("takeEnded")
                assert not any(event["event"] == "fixtureWarmRouteOpen" for event in events[before_switch:])
                restored = wait_for("fixtureWarmRouteOpen")
                assert restored["uid"] == "fixture-saved" and not restored["failing"]

                saved_path = root / "saved.wav"
                send(json.dumps({"start": str(saved_path), "preRoll": False}))
                started = wait_for("started")
                assert started["microphone"] == "Fixture saved microphone" and not started["usingDefaultMicrophone"]
                wait_for("levels")
                time.sleep(.32)
                send("stop")
                assert wait_for("finished")["seconds"] >= .3
                wait_for("takeEnded")
                for path in (fallback_path, saved_path):
                    with wave.open(str(path), "rb") as take:
                        assert any(take.readframes(take.getnframes()))
                helper.stdin.close()
                assert helper.wait(timeout=5) == 0
                print("A warm fallback kept recording through its tail and restored the saved input between takes.")
            finally:
                if helper.poll() is None:
                    helper.kill()


def exercise_lid_transition(binary):
    with tempfile.TemporaryDirectory(prefix="jaro-warm-lid-") as folder:
        root = Path(folder)
        lid = root / "lid-closed"
        env = {**os.environ, "JARD_TEST_MICROPHONE_LID_GATE": str(lid)}
        with subprocess.Popen([binary, "warm", "--input", "fixture"], env=env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as helper:
            def wait_for(kind):
                while line := helper.stdout.readline():
                    event = json.loads(line)
                    assert event["event"] != "error", event
                    if event["event"] == kind:
                        return event
                raise AssertionError(f"Missing {kind} during the lid transition.")

            def send(command):
                helper.stdin.write(command + "\n")
                helper.stdin.flush()

            def start(name, microphone, fallback=False):
                path = root / name
                send(json.dumps({"start": str(path), "preRoll": False}))
                event = wait_for("started")
                assert event["microphone"] == microphone and event["usingDefaultMicrophone"] == fallback, event
                wait_for("levels")
                time.sleep(.36)
                return path

            try:
                wait_for("warmReady")
                assert wait_for("fixtureWarmRouteOpen")["uid"] == "fixture"
                interrupted_path = start("interrupted.wav", "Fixture system microphone")
                lid.touch()
                interrupted = wait_for("finished")
                assert interrupted["interruption"] == "microphone" and interrupted["hasLiveAudio"]
                assert "lid is closed" in interrupted["reason"]
                wait_for("takeEnded")
                assert wait_for("fixtureWarmRouteOpen")["uid"] == "fixture-usb"

                fallback_path = start("fallback.wav", "Fixture USB microphone", fallback=True)
                lid.unlink()
                time.sleep(.4)
                send("stop")
                closed = wait_for("finished")
                assert "interruption" not in closed and closed["seconds"] >= .7
                wait_for("takeEnded")
                assert wait_for("fixtureWarmRouteOpen")["uid"] == "fixture"
                restored_path = start("restored.wav", "Fixture system microphone")
                send("stop")
                wait_for("finished")
                wait_for("takeEnded")
                for path in (interrupted_path, fallback_path, restored_path):
                    with wave.open(str(path), "rb") as take:
                        assert any(take.readframes(take.getnframes()))
                helper.stdin.close()
                assert helper.wait(timeout=5) == 0
                print("A lid closure retained the interrupted prefix, a new take used USB, and reopening restored the saved input.")
            finally:
                if helper.poll() is None:
                    helper.kill()


def exercise_slow_opens(binary, *, on_demand=False):
    with tempfile.TemporaryDirectory(prefix="jaro-warm-slow-open-") as folder:
        root = Path(folder)
        gate = root / "microphone-ready"
        env = {**os.environ, "JARD_TEST_WARM_OPEN_GATE": str(gate)}
        command = [binary, "warm", *(["--on-demand"] if on_demand else [])]
        with subprocess.Popen(command, env=env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as helper:
            def wait_for(kind):
                while line := helper.stdout.readline():
                    event = json.loads(line)
                    assert event["event"] != "error", event
                    if event["event"] == kind:
                        return event
                raise AssertionError(f"Missing {kind} while opening the microphone.")

            def send(command):
                helper.stdin.write(command + "\n")
                helper.stdin.flush()

            def start(path):
                send(json.dumps({"start": str(path), "preRoll": False}))

            try:
                wait_for("warmReady")
                path = root / "abandoned.wav"
                if on_demand:
                    start(path)
                wait_for("fixtureWarmOpening")
                for index, command in enumerate(("stop", "cancel", f"finish {time.perf_counter():.9f}")):
                    if not on_demand or index:
                        start(path)
                    send(command)
                    wait_for("openingAbandoned")
                    wait_for("takeEnded")
                    assert not path.exists() and not gate.exists()

                late_path = root / "late-release.wav"
                start(late_path)
                early_release = time.perf_counter()
                gate.touch()
                wait_for("started")
                send(f"finish {early_release:.9f}")
                wait_for("openingAbandoned")
                assert wait_for("finished")["seconds"] == 0
                wait_for("takeEnded")

                fresh = root / "fresh.wav"
                start(fresh)
                wait_for("started")
                wait_for("levels")
                time.sleep(.32)
                send("stop")
                assert wait_for("finished")["seconds"] >= .3
                wait_for("takeEnded")
                helper.stdin.close()
                assert helper.wait(timeout=5) == 0
                mode = "on-demand" if on_demand else "warm"
                print(f"Stop, Cancel, and earlier shortcut releases abandoned slow {mode} opens, and a fresh take succeeded.")
            finally:
                if helper.poll() is None:
                    helper.kill()


def exercise_slow_replacement(binary, route_changes):
    with tempfile.TemporaryDirectory(prefix="jaro-warm-replacement-") as folder:
        root = Path(folder)
        gate = root / "open-ready"
        env = {**os.environ, "JARD_TEST_WARM_OPEN_GATE": str(gate)}
        if route_changes:
            lid = root / "lid-closed"
            env["JARD_TEST_MICROPHONE_LID_GATE"] = str(lid)
            env["JARD_TEST_WARM_GATED_UID"] = "fixture"
            args = [binary, "warm", "--input", "fixture"]
        else:
            allowed = root / "saved-listed"
            env["JARD_TEST_WARM_SAVED_INPUT_GATE"] = str(allowed)
            env["JARD_TEST_WARM_GATED_UID"] = "fixture-saved"
            args = [binary, "warm", "--input", "fixture-saved"]
        with subprocess.Popen(args, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as helper:
            def wait_for(kind):
                while line := helper.stdout.readline():
                    event = json.loads(line)
                    assert event["event"] != "error", event
                    if event["event"] == kind:
                        return event
                raise AssertionError(f"Missing {kind} during a delayed route replacement.")

            def send(command):
                helper.stdin.write(command + "\n")
                helper.stdin.flush()

            try:
                wait_for("warmReady")
                if route_changes:
                    assert wait_for("fixtureWarmOpening")["uid"] == "fixture"
                    lid.touch()
                else:
                    assert wait_for("fixtureWarmRouteOpen")["failing"]
                    assert wait_for("fixtureWarmRouteOpen")["uid"] == "fixture"
                    allowed.touch()
                    assert wait_for("fixtureWarmOpening")["uid"] == "fixture-saved"

                output = root / "fallback.wav"
                send(json.dumps({"start": str(output), "preRoll": False}))
                if route_changes:
                    gate.touch()
                started = wait_for("started")
                assert started["usingDefaultMicrophone"]
                assert started["microphone"] == ("Fixture USB microphone" if route_changes else "Fixture system microphone")
                if not route_changes:
                    assert not gate.exists(), "The existing fallback must work while the preferred input is still blocked."
                    gate.touch()
                wait_for("levels")
                time.sleep(.32)
                send(f"finish {time.perf_counter():.9f}")
                assert wait_for("finished")["seconds"] >= .3
                wait_for("takeEnded")
                if not route_changes:
                    restored = wait_for("fixtureWarmRouteOpen")
                    assert restored["uid"] == "fixture-saved" and not restored["failing"]
                    send(json.dumps({"start": str(root / "restored.wav"), "preRoll": False}))
                    assert not wait_for("started")["usingDefaultMicrophone"]
                    time.sleep(.32)
                    send("stop")
                    wait_for("finished")
                    wait_for("takeEnded")
                helper.stdin.close()
                assert helper.wait(timeout=5) == 0
            finally:
                if helper.poll() is None:
                    helper.kill()


run(sys.argv[1])
exercise_annotation_narration(sys.argv[1])
exercise_on_demand_annotation(sys.argv[1])
exercise_shared_meeting(sys.argv[1])
exercise_standalone_meeting(sys.argv[1])
exercise_zero_prefix(sys.argv[1])
exercise_failed_open(sys.argv[1])
exercise_saved_input_fallback(sys.argv[1])
exercise_lid_transition(sys.argv[1])
exercise_slow_opens(sys.argv[1])
exercise_slow_opens(sys.argv[1], on_demand=True)
exercise_slow_replacement(sys.argv[1], route_changes=False)
exercise_slow_replacement(sys.argv[1], route_changes=True)
print("A working fallback recorded during a slow probe, and a route change rejected the old device result.")
