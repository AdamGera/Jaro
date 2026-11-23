"""Keep undo state with PulseAudio and MPRIS changes until the owner closes stdin."""

import asyncio
import json
import sys


CALL_TIMEOUT = .4
MPRIS = "org.mpris.MediaPlayer2"
PLAYER = MPRIS + ".Player"
PLAYER_PATH = "/org/mpris/MediaPlayer2"
BUS = "org.freedesktop.DBus"


async def pactl(*args):
    process = await asyncio.create_subprocess_exec(
        "pactl", *map(str, args), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        result, _ = await asyncio.wait_for(process.communicate(), CALL_TIMEOUT)
    except TimeoutError:
        return None
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    return result if process.returncode == 0 else None


async def pulse_list(kind):
    payload = await pactl("--format=json", "list", kind)
    return json.loads(payload) if payload else []


def stream_identity(stream):
    properties = stream.get("properties", {})
    return (stream["index"], stream["client"], properties.get("object.serial"),
            properties.get("application.process.id"))


def volume(stream):
    return tuple(channel["value"] for channel in stream["volume"].values())


async def control_pulse(behavior, percent, ended, ready):
    name = await pactl("get-default-sink")
    sink = next((item["index"] for item in await pulse_list("sinks")
                 if item["name"] == (name or b"").decode().strip()), None)
    if sink is None:
        return
    seen, changed = set(), {}
    field = "mute" if behavior == "mute" else "volume"
    command = f"set-sink-input-{field}"
    try:
        while not ended.done():
            for stream in await pulse_list("sink-inputs"):
                if ended.done():
                    break
                if stream.get("sink") != sink or stream.get("properties", {}).get("application.name") == "Jaro":
                    continue
                key = stream_identity(stream)
                if key in seen:
                    continue
                seen.add(key)
                before = (int(stream["mute"]),) if behavior == "mute" else volume(stream)
                target = (1,) if behavior == "mute" else tuple(round(channel * percent / 100) for channel in before)
                if before != target and await pactl(command, key[0], *target) is not None:
                    changed[key] = before, target
            ready()
            await asyncio.wait((ended,), timeout=.3)
    finally:
        if changed:
            for stream in await pulse_list("sink-inputs"):
                key = stream_identity(stream)
                if key not in changed:
                    continue
                before, target = changed[key]
                current = (int(stream["mute"]),) if behavior == "mute" else volume(stream)
                if current == target:
                    await pactl(command, key[0], *before)


async def control_players(ended, ready):
    from dbus_fast import Message, MessageType
    from dbus_fast.aio import MessageBus

    bus = await asyncio.wait_for(MessageBus().connect(), CALL_TIMEOUT)

    async def call(destination, path, interface, member, signature="", body=None):
        try:
            reply = await asyncio.wait_for(bus.call(Message(destination=destination, path=path, interface=interface,
                                                      member=member, signature=signature, body=body or [])), CALL_TIMEOUT)
        except TimeoutError:
            return None
        return reply.body if reply and reply.message_type == MessageType.METHOD_RETURN else None

    async def properties(owner):
        reply = await call(owner, PLAYER_PATH, BUS + ".Properties", "GetAll", "s", [PLAYER])
        return {key: value.value for key, value in reply[0].items()} if reply else {}

    async def pause(name):
        owner = await call(BUS, "/org/freedesktop/DBus", BUS, "GetNameOwner", "s", [name])
        if not owner:
            return None
        before = await properties(owner[0])
        if ended.done() or before.get("PlaybackStatus") != "Playing":
            return None
        if await call(owner[0], PLAYER_PATH, PLAYER, "Pause") is not None:
            return owner[0], before.get("Metadata", {}).get("mpris:trackid")
        return None

    async def resume(owner, track):
        after = await properties(owner)
        if after.get("PlaybackStatus") == "Paused" and after.get("Metadata", {}).get("mpris:trackid") == track:
            await call(owner, PLAYER_PATH, PLAYER, "Play")

    paused = []
    try:
        names = await call(BUS, "/org/freedesktop/DBus", BUS, "ListNames")
        paused = await asyncio.gather(*(pause(name) for name in names[0] if name.startswith(MPRIS + "."))) if names else []
        ready()
        await ended
    finally:
        await asyncio.gather(*(resume(*entry) for entry in paused if entry))
        bus.disconnect()


async def run(behavior, percent):
    ended = asyncio.create_task(asyncio.to_thread(sys.stdin.buffer.read, 1))
    announced = False

    def ready():
        nonlocal announced
        if not announced:
            announced = True
            try:
                print("ready", flush=True)
            except BrokenPipeError:
                pass

    try:
        if behavior == "pause":
            await control_players(ended, ready)
        else:
            await control_pulse(behavior, percent, ended, ready)
    finally:
        ready()


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("mute", "lower", "pause") or not 0 <= int(sys.argv[2]) <= 100:
        sys.exit(2)
    asyncio.run(run(sys.argv[1], int(sys.argv[2])))
