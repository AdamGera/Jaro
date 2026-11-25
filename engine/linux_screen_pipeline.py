"""Feed independent Pulse inputs to a screen recorder on its GStreamer clock."""

import json
import signal
import sys
import time


def load_gstreamer():
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    return Gst


class Input:
    def __init__(self, gst, clock, name, device, caps):
        self.gst, self.name, self.device = gst, name, device
        self.pipeline = gst.parse_launchv([
            "pulsesrc", "name=input", "provide-clock=false", "!",
            "audioconvert", "!", "audioresample", "!", caps, "!",
            "interaudiosink", f"channel={name}", "sync=false", "async=false",
        ])
        self.pipeline.use_clock(clock)
        self.bus = self.pipeline.get_bus()
        self.source = self.pipeline.get_by_name("input")
        self.source.set_property("client-name", "Jaro Screen Recording")
        self.source.set_property("device", device)
        props, _ = gst.Structure.from_string("props,node.dont-reconnect=(string)true,node.dont-fallback=(string)true")
        self.source.set_property("stream-properties", props)
        self.source.get_static_pad("src").add_probe(gst.PadProbeType.BUFFER, self.check_route)
        self.moved = False
        self.retry_at = 0

    def check_route(self, _pad, _info):
        actual = self.source.get_property("current-device")
        if actual == self.device and not self.moved:
            return self.gst.PadProbeReturn.OK
        if actual:
            self.moved = True
        return self.gst.PadProbeReturn.DROP

    def update(self, now):
        gst = self.gst
        if self.retry_at:
            if now < self.retry_at:
                return
            self.retry_at = 0
            self.moved = False
            self.pipeline.set_state(gst.State.PLAYING)
        failed = self.moved
        while message := self.bus.pop():
            if message.type in (gst.MessageType.ERROR, gst.MessageType.EOS):
                failed = True
            elif (message.type == gst.MessageType.STATE_CHANGED and message.src == self.pipeline
                  and message.parse_state_changed().newstate == gst.State.PLAYING and not failed):
                print(f"jaro-screen-audio {self.name} ready", flush=True)
        if failed:
            self.close()
            self.retry_at = now + 1
            print(f"jaro-screen-audio {self.name} unavailable", flush=True)

    def close(self):
        self.pipeline.set_state(self.gst.State.NULL)


def record(gst, arguments, devices):
    movie = gst.parse_launchv(arguments)
    clock = gst.SystemClock.obtain()
    movie.use_clock(clock)
    bus = movie.get_bus()
    inputs = [Input(gst, clock, name, device, caps) for name, (device, caps) in devices.items()]
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    sent_eos = False
    try:
        movie.set_state(gst.State.PLAYING)
        for source in inputs:
            source.retry_at = time.monotonic()
        while True:
            if stopping and not sent_eos:
                movie.send_event(gst.Event.new_eos())
                sent_eos = True
            message = bus.timed_pop(20 * gst.MSECOND)
            if message and message.type == gst.MessageType.ERROR:
                error, debug = message.parse_error()
                print(f"{error}: {debug}", file=sys.stderr)
                return 1
            if message and message.type == gst.MessageType.EOS:
                return 0
            if message and message.type == gst.MessageType.ELEMENT:
                structure = message.get_structure()
                if structure and structure.get_name() == "level":
                    # Use the same GStreamer structure syntax as gst-launch's level messages.
                    print(f'from element "{message.src.get_name()}" (element): {structure.to_string()}', flush=True)
            if not stopping:
                now = time.monotonic()
                for source in inputs:
                    source.update(now)
    finally:
        movie.set_state(gst.State.NULL)
        for source in inputs:
            source.close()


if __name__ == "__main__":
    try:
        gst = load_gstreamer()
    except (ImportError, ValueError) as error:
        print(error, file=sys.stderr)
        sys.exit(3)
    sys.exit(record(gst, sys.argv[2:], json.loads(sys.argv[1])))
