"""System dictation through a lifecycle-owned Windows keyboard and paste helper."""
import asyncio
from contextlib import suppress
import logging
import os
from pathlib import Path
from secrets import token_hex
from struct import Struct
import subprocess
from time import monotonic

from starlette.concurrency import run_in_threadpool

from desktop_clipboard import copy_text
from dictation_output import canonical_host
from portable_capture import CaptureError
from portable_shortcuts import shortcut_parts
from portable_dictation import HISTORY_ERRORS, OutputContext, QUEUE_FULL, PortableDictation, join_dictation


COPY_LAST = "Dictation is ready. Use Copy last to paste it into your app."
ELEVATED_COPY_LAST = "This app runs with higher Windows privileges. Use Copy last to paste the dictation manually."
PASTE_CONTINUATION_CHANGED = 6
PASTE_TARGET_ELEVATED = 7
PASTE_REQUEST = Struct("<QQIIII32s")
FOCUS_REQUEST = Struct("<cI")
PICKER_HEADER = Struct("<cII")
MAX_PASTE_BYTES = 128 * 1024
PICKER_LIMIT = 8
MODES = ("hold", "tap", "holdDouble", "doubleTap")
DEFAULT_SHORTCUT = "Left Alt"
MODIFIER_MASKS = {"Alt": 1, "Ctrl": 2, "Shift": 4, "Super": 8}
VIRTUAL_KEYS = {
    "BackSpace": 0x08, "Tab": 0x09, "Return": 0x0D, "Escape": 0x1B, "space": 0x20,
    "Page_Up": 0x21, "Page_Down": 0x22, "End": 0x23, "Home": 0x24, "Left": 0x25,
    "Up": 0x26, "Right": 0x27, "Down": 0x28, "Print": 0x2C, "Insert": 0x2D, "Delete": 0x2E,
}
logger = logging.getLogger(__name__)


def windows_shortcut_parts(value):
    value = value.strip().casefold()
    if value == DEFAULT_SHORTCUT.casefold():
        return DEFAULT_SHORTCUT, ()
    label, modifiers, keysym = shortcut_parts(value.replace("left alt+", "alt+"))
    mask = sum(MODIFIER_MASKS[modifier] for modifier in modifiers)
    if len(keysym) == 1:
        key = ord(keysym.upper())
    elif keysym.startswith("F") and keysym[1:].isdigit():
        key = 0x6F + int(keysym[1:])
    else:
        key = VIRTUAL_KEYS[keysym]
    if (key, mask) == (ord("V"), 3):
        raise CaptureError("This shortcut is reserved for Paste last dictation.")
    if (key == 0x2E and mask & 3 == 3) or (key == ord("L") and mask & 8):
        raise CaptureError("This shortcut is reserved by Windows.")
    return label, (str(key), str(mask))


def helper_path():
    override = os.environ.get("JARO_WINDOWS_DICTATION")
    if override:
        path = Path(override)
        if path.is_absolute() and path.is_file():
            return path
    return None


async def stop_process(process):
    process.stdin.close()
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()


def window_process(window):
    import ctypes
    from ctypes import wintypes

    user = ctypes.WinDLL("user32")
    kernel = ctypes.WinDLL("kernel32")
    user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                  ctypes.POINTER(wintypes.DWORD))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    pid = wintypes.DWORD()
    if not window or not user.GetWindowThreadProcessId(window, ctypes.byref(pid)):
        return None
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        path = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(path))
        if kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)):
            return pid.value, path.value
    finally:
        kernel.CloseHandle(handle)
    return None


def windowed_applications():
    import ctypes
    from ctypes import wintypes

    paths = set()
    user = ctypes.WinDLL("user32")
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.IsWindowVisible.argtypes = (wintypes.HWND,)
    user.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
    user.GetWindow.restype = wintypes.HWND
    user.EnumWindows.argtypes = (callback, wintypes.LPARAM)

    @callback
    def visit(window, _data):
        if user.IsWindowVisible(window) and not user.GetWindow(window, 4):  # GW_OWNER
            if process := window_process(window):
                paths.add(process[1])
        return True

    user.EnumWindows(visit, 0)
    return paths


def foreground_application():
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    user = ctypes.WinDLL("user32")
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    window = user.GetForegroundWindow()
    if process := window_process(window):
        title = ctypes.create_unicode_buffer(512)
        if 0 < user.GetWindowTextLengthW(window) < len(title):
            user.GetWindowTextW(window, title, len(title))
        if user.GetForegroundWindow() == window:
            return window, process[0], Path(process[1]).stem[:256], title.value
    return None


class WindowsDictation(PortableDictation):
    platform = "windows"
    shortcut = DEFAULT_SHORTCUT
    recovery_shortcut = "Ctrl+Left Alt+V"

    def __init__(self, state, transcribe, history):
        self.listener = None
        self.cancel_shortcut_armed = False
        super().__init__(state, transcribe, history)
        self.paste_worker = None
        self.events = None
        self.focus = None
        self.focus_revision = 0
        self.activity_channel = None
        self.input_revision = 0
        self.take_input_revision = 0
        self.last_insertion = None
        self.recovery_focus = None
        self.picker_sequence = None
        self.picker_rows = ()
        self.picker_load = None
        self.gesture_accepted = False
        self.command_focus = None
        self.command_sequence = 0
        self.changing_listener = False
        self.raw_shortcut = ""
        self.raw_shortcut_error = ""
        try:
            binding = history.desktop_shortcut(windows=True)
            if isinstance(binding, str) and binding:
                self.shortcut = windows_shortcut_parts(binding)[0]
        except (*HISTORY_ERRORS, CaptureError):
            logger.warning("Could not read the Windows dictation shortcut.", exc_info=True)
        try:
            binding = history.desktop_shortcut(windows=True, raw=True)
            if isinstance(binding, str) and binding:
                self.raw_shortcut = self.validate_raw_shortcut(binding)
        except (CaptureError, *HISTORY_ERRORS) as failure:
            self.raw_shortcut_error = str(failure) if isinstance(failure, CaptureError) else "Could not read the raw dictation shortcut."
        self.mode = "hold"
        try:
            stored = history.windows_shortcut_mode()
            if stored in MODES:
                self.mode = stored
        except HISTORY_ERRORS:
            logger.warning("Could not read the Windows dictation shortcut mode.", exc_info=True)

    @property
    def enabled(self):
        return self.listener is not None

    async def spawn(self, *arguments):
        helper = helper_path()
        if helper is None:
            raise CaptureError("Jaro’s Windows shortcut helper is missing. Rebuild the desktop host.")
        return await asyncio.create_subprocess_exec(
            helper, *arguments, creationflags=subprocess.CREATE_NO_WINDOW,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=2048,
        )

    async def enable(self):
        if self.listener is not None:
            return
        self.authorizing = True
        helper = None
        channel = token_hex(16)
        try:
            chord = windows_shortcut_parts(self.shortcut)[1]
            raw = ("--raw", *windows_shortcut_parts(self.raw_shortcut)[1]) if self.raw_shortcut else ()
            helper = await self.spawn("listen", self.mode, channel, *chord, *raw)
            if (await asyncio.wait_for(helper.stdout.readline(), 4)).strip() != b"ready":
                raise CaptureError("Windows did not start Jaro’s keyboard shortcut.")
            self.listener = helper
            self.activity_channel = channel
            self.input_revision = 0
            self.update_cancel_shortcut()
            self.events = asyncio.create_task(self.read_events(helper))
            self.feedback = ""
        except (OSError, TimeoutError) as failure:
            raise CaptureError("Windows did not start Jaro’s keyboard shortcut.") from failure
        finally:
            self.authorizing = False
            if helper is not None and self.listener is None:
                await stop_process(helper)

    async def read_events(self, helper):
        try:
            while event := await helper.stdout.readline():
                await self.handle_event(event.decode("utf-8", errors="replace").rstrip("\r\n"))
        except (OSError, ValueError):
            pass
        finally:
            if self.listener is helper:
                if self.command_focus and not self.command_focus[2].done():
                    self.command_focus[2].set_result(False)
                async with self.control:
                    if self.listener is helper:
                        self.listener = None
                        self.cancel_shortcut_armed = False
                        self.activity_channel = None
                        self.last_insertion = None
                        self.gesture_accepted = False
                        self.close_picker()
                        await stop_process(helper)
                        try:
                            await self.cancel_capture(include_pending=True)
                        finally:
                            await self.close_paste_worker()
                        self.feedback = "The Windows shortcut disconnected. Enable it again to reconnect."

    def snapshot(self):
        return {**super().snapshot(), "recoveryShortcut": self.recovery_shortcut, "shortcutMode": self.mode,
                "defaultShortcut": DEFAULT_SHORTCUT, "rawShortcut": self.raw_shortcut,
                "rawShortcutError": self.raw_shortcut_error,
                "cancelShortcut": "Esc" if self.enabled else ""}

    async def change_mode(self, mode):
        if mode != self.mode:
            await self.change_listener(
                mode, self.shortcut, self.raw_shortcut, lambda: self.history.configure(windows_shortcut_mode=mode),
                "Finish the current dictation before changing the shortcut mode.",
                "Could not change the Windows dictation shortcut mode.",
            )

    def validate_raw_shortcut(self, value):
        label, chord = windows_shortcut_parts(value)
        if not chord:
            raise CaptureError("Choose a key chord for dictation without processing.")
        if chord == windows_shortcut_parts(self.shortcut)[1]:
            raise CaptureError("Choose different shortcuts for dictation and dictation without processing.")
        return label

    async def change_shortcut(self, value, *, raw=False):
        if raw:
            shortcut = self.validate_raw_shortcut(value) if value else ""
            if shortcut and (annotation := getattr(self.state, "desktop_annotation", None)):
                annotation.check_dictation(shortcut)
            if shortcut != self.raw_shortcut or self.raw_shortcut_error:
                await self.change_listener(
                    self.mode, self.shortcut, shortcut,
                    lambda: self.history.save_desktop_shortcut(shortcut, windows=True, raw=True),
                    "Finish the current dictation before changing the shortcut.",
                    "Could not change the Windows raw dictation shortcut.",
                )
                self.raw_shortcut_error = ""
            return
        shortcut, _ = windows_shortcut_parts(value)
        if shortcut == self.raw_shortcut:
            raise CaptureError("Choose different shortcuts for dictation and dictation without processing.")
        if annotation := getattr(self.state, "desktop_annotation", None):
            annotation.check_dictation(shortcut)
        if shortcut != self.shortcut:
            await self.change_listener(
                self.mode, shortcut, self.raw_shortcut, lambda: self.history.save_desktop_shortcut(shortcut, windows=True),
                "Finish the current dictation before changing the shortcut.",
                "Could not change the Windows dictation shortcut.",
            )

    async def change_listener(self, mode, shortcut, raw_shortcut, save, busy_message, failure_message):
        if self.busy or self.gesture_accepted or (self.action is not None and not self.action.done()):
            raise CaptureError(busy_message)
        previous = self.mode, self.shortcut, self.raw_shortcut
        listening = self.enabled
        self.changing_listener = True
        try:
            if listening:
                await self.disable()
            self.mode, self.shortcut, self.raw_shortcut = mode, shortcut, raw_shortcut
            if listening:
                await self.enable()
            await run_in_threadpool(save)
        except (CaptureError, *HISTORY_ERRORS) as failure:
            if self.enabled:
                await self.disable()
            self.mode, self.shortcut, self.raw_shortcut = previous
            if listening:
                try:
                    await self.enable()
                except CaptureError:
                    logger.warning("Could not restore the Windows dictation shortcut.", exc_info=True)
            raise CaptureError(failure_message) from failure
        finally:
            self.changing_listener = False

    def reset_gesture(self):
        if self.listener is not None:
            with suppress(OSError, RuntimeError):
                self.listener.stdin.write(b"r")

    def update_cancel_shortcut(self):
        if self.listener is None:
            return
        armed = not self.state.draining and self.can_cancel and self.phase != "idle"
        if self.cancel_shortcut_armed != armed:
            with suppress(OSError, RuntimeError):
                self.listener.stdin.write(b"c" if armed else b"i")
                self.cancel_shortcut_armed = armed

    async def clear_capture(self, *, keep_focus=False):
        if self.gesture_accepted:
            self.gesture_accepted = False
            self.reset_gesture()
        await super().clear_capture(keep_focus=keep_focus)

    def set_capture_target(self, focus, application):
        if self.focus != focus:
            self.clear_focus()
            self.focus = focus
        self.take_input_revision = self.input_revision
        self.application = application.removesuffix(".exe")[:256]

    async def output_context(self, read_site, *, annotation=False):
        from portable_calls import BROWSER_CONTEXT_BINARIES
        from windows_capture import capture_inventory

        focus = foreground_application()
        if focus is None:
            return OutputContext()
        window, pid, application, title = focus
        if read_site and title and application.casefold() in BROWSER_CONTEXT_BINARIES:
            try:
                output = await capture_inventory("foreground-page", str(window), str(pid), timeout=.35)
                line = output.decode("utf8").removesuffix("\n")
                prefix, _, address = line.partition("\t0\t0\t")
                if prefix == "U" and address.startswith(("https://", "http://")) and foreground_application() == focus:
                    return OutputContext(application, site_url=address) if annotation else OutputContext(application, canonical_host(address))
            except (CaptureError, UnicodeError):
                pass
        return OutputContext(application)

    @staticmethod
    def event_target(fields):
        if len(fields) < 3 or not all(part.isascii() and part.isdecimal() for part in fields[:3]):
            return None
        return tuple(fields[:3]) if fields[0] != "0" and fields[2] != "0" else None

    async def request_command_focus(self, *, recovery=False):
        self.command_sequence = (self.command_sequence + 1) & 0xFFFFFFFF
        reply = asyncio.get_running_loop().create_future()
        self.command_focus = (self.command_sequence, recovery, reply)
        try:
            async with asyncio.timeout(1):
                self.listener.stdin.write(FOCUS_REQUEST.pack(b"p" if recovery else b"f", self.command_sequence))
                await self.listener.stdin.drain()
                if not await reply:
                    raise CaptureError("The Windows shortcut disconnected.")
        except (OSError, RuntimeError, TimeoutError) as failure:
            raise CaptureError("Could not get the active Windows input field.") from failure
        finally:
            self.command_focus = None
            reply.cancel()

    async def run_command(self, action, *, raw=False, printed=False):
        if printed:
            return await self.print_current_take()
        async with self.control:
            if self.state.draining:
                raise CaptureError("Jaro is preparing to restart.")
            if not self.enabled:
                raise CaptureError("Enable the Windows dictation shortcut in Jaro first.")
            if action == "cancel":
                await self.cancel_dictation()
                return {"status": self.phase}
            if self._capture_phase == "starting" and action in ("stop", "toggle"):
                if self.action is not None:
                    try:
                        await asyncio.wait_for(asyncio.shield(self.action), 5)
                    except TimeoutError as failure:
                        raise CaptureError("Jaro is still opening the microphone.") from failure
            if action == "paste-last":
                if self.busy or not self.can_start_recording:
                    raise CaptureError("Finish the current dictation before pasting the last one.")
                if not (await run_in_threadpool(self.snapshot))["text"]:
                    raise CaptureError("There’s no last dictation to paste.")
                if self.busy or not self.can_start_recording:
                    raise CaptureError("Finish the current dictation before pasting the last one.")
                await self.request_command_focus(recovery=True)
                if self.state.draining:
                    raise CaptureError("Jaro is preparing to restart.")
                self.phase = "pasting"
                self.action = asyncio.create_task(self.paste_last())
                return {"status": "pasting"}
            if self._capture_phase == "recording":
                if action == "start" or (action == "toggle" and raw != self.capture_raw):
                    return {"status": "recording"}
                if not self.schedule():
                    raise CaptureError("Jaro is already finishing this dictation.")
            elif action == "stop":
                raise CaptureError("There is no active dictation.")
            elif self.can_start_recording:
                state = self.state
                if state.status != "ready" or state.portable_recording or getattr(state, "portable_screencast", None):
                    raise CaptureError("Dictation is unavailable while Jaro starts or records audio.")
                await self.request_command_focus()
                if self.state.draining:
                    raise CaptureError("Jaro is preparing to restart.")
                if not self.schedule(raw=raw):
                    raise CaptureError("Jaro could not start another dictation.")
                return {"status": "starting"}
            else:
                raise CaptureError(QUEUE_FULL if self.queue_full else "Finish the current dictation first.")
        return {"status": "transcribing"}

    async def handle_event(self, event):
        action = event.partition(" ")[0]
        if action == "activity":
            revision = event.removeprefix("activity ")
            if revision.isascii() and revision.isdecimal() and len(revision) <= 10:
                self.input_revision = int(revision)
                self.last_insertion = None
        elif action == "command-focus":
            fields = event.split(" ", 5)
            if self.command_focus is not None and len(fields) >= 5:
                sequence, recovery, reply = self.command_focus
                if fields[1] == str(sequence) and not reply.done():
                    focus = self.event_target(fields[2:5])
                    if recovery:
                        self.recovery_focus = focus
                    else:
                        self.set_capture_target(focus, fields[5] if len(fields) == 6 else "")
                    reply.set_result(True)
        elif action == "paste-picker":
            fields = event.split()
            if len(fields) == 5 and fields[1].isascii() and fields[1].isdecimal() and len(fields[1]) <= 10:
                sequence = int(fields[1])
                if 0 < sequence <= 0xFFFFFFFF:
                    self.close_picker()
                    self.picker_sequence = sequence
                    self.recovery_focus = self.event_target(fields[2:5])
                    self.picker_load = asyncio.create_task(self.load_picker(sequence))
        elif action in ("picker-paste", "picker-copy"):
            fields = event.split()
            if len(fields) == 3 and fields[1] == str(self.picker_sequence) and fields[2] in (
                    str(index) for index in range(len(self.picker_rows))):
                entry = self.picker_rows[int(fields[2])]
                self.close_picker()
                if self.recovery_focus is not None and self.can_start_recording and not self.busy and not self.state.draining:
                    self.phase = "pasting"
                    self.action = asyncio.create_task(self.apply_picker_entry(entry, copy=action == "picker-copy"))
        elif action in ("start", "start-raw", "paste-last"):
            starting = action != "paste-last"
            if self.changing_listener or self.command_focus:
                if starting:
                    self.reset_gesture()
                return
            available = self.can_start_recording and (starting or not self.jobs)
            if available and not self.state.draining:
                fields = event.split(" ", 4)
                if len(fields) < 4 or not all(part.isascii() and part.isdecimal() for part in fields[1:4]):
                    return
                focus = self.event_target(fields[1:4])
                if action == "paste-last":
                    self.recovery_focus = focus
                    self.phase = "pasting"
                    self.action = asyncio.create_task(self.paste_last())
                    return
                self.set_capture_target(focus, fields[4] if len(fields) == 5 else "")
                self.gesture_accepted = True
                self.schedule(raw=action == "start-raw")
            elif starting and not self.gesture_accepted:
                if self.queue_full:
                    self.feedback = QUEUE_FULL
                self.reset_gesture()
        elif event == "focus-lost":
            self.clear_focus()
            self.last_insertion = None
        elif event == "paste-focus-lost":
            self.close_picker()
            if self.recovery_focus is not None:
                self.last_insertion = None
            self.recovery_focus = None
        elif event == "escape":
            async with self.control:
                if not self.state.draining and await self.cancel_dictation():
                    self.gesture_accepted = False
        elif event in ("finish", "cancel") and self.gesture_accepted:
            self.gesture_accepted = False
            if event == "finish" and self.phase == "recording":
                # The recorder is already running. Keep the target through inference.
                self.schedule(released_at=monotonic())
            elif event == "cancel":
                async with self.control:
                    if not self.state.draining:
                        await self.cancel_dictation()
            elif self.phase in ("idle", "starting"):
                await self.cancel_capture()

    def clear_focus(self):
        self.focus = None
        self.focus_revision += 1

    def close_picker(self):
        self.picker_sequence = None
        self.picker_rows = ()
        if self.picker_load is not None:
            self.picker_load.cancel()
            self.picker_load = None

    def recent_picker_rows(self):
        text = self.snapshot()["text"]
        try:
            recent = self.history.recent_paste_entries(PICKER_LIMIT)
        except HISTORY_ERRORS:
            recent = ()
        rows = [(self.last_id, text)] if text else []
        rows.extend((key, value) for key, value in recent if key != self.last_id or not text)
        return tuple((key, value) for key, value in rows if picker_text(value))[:PICKER_LIMIT]

    async def load_picker(self, sequence):
        listener = self.listener
        if listener is None:
            return
        try:
            available = self.recovery_focus is not None and self.can_start_recording and not self.busy and not self.state.draining
            rows = await run_in_threadpool(self.recent_picker_rows) if available else ()
            if self.picker_sequence != sequence or self.listener is not listener:
                return
            values = [picker_text(text) for _, text in rows]
            data = PICKER_HEADER.pack(b"k", sequence, len(rows))
            data += b"".join(len(value).to_bytes(4, "little") for value in values) + b"".join(values)
            self.picker_rows = rows
            listener.stdin.write(data)
            await listener.stdin.drain()
        except (OSError, RuntimeError):
            self.picker_rows = ()

    async def apply_picker_entry(self, entry, *, copy):
        focus = self.recovery_focus
        try:
            available = await run_in_threadpool(self.recent_picker_rows)
            if not focus or self.recovery_focus is not focus or self.state.draining:
                return
            if entry not in available:
                self.feedback = "That dictation is no longer in Paste Last."
                return
            text = entry[1]
            if copy:
                try:
                    await copy_text(text)
                    self.feedback = "Dictation copied."
                except (OSError, ValueError):
                    self.feedback = "Could not copy that dictation."
            else:
                # An explicit selection pastes the text shown in the preview.
                self.feedback = await self.paste_to(text, focus, lambda: self.recovery_focus is focus,
                                                    self.terminal, self.input_revision, raw=True)
        finally:
            self.recovery_focus = None
            self.phase = "idle"

    async def paste_last(self):
        try:
            text = (await run_in_threadpool(self.snapshot))["text"]
            self.feedback = await self.deliver(text, recovery=True) if text else "There’s no last dictation to paste."
        finally:
            self.recovery_focus = None
            self.phase = "idle"

    async def copy_interrupted_text(self, text):
        self.last_insertion = None
        return await super().copy_interrupted_text(text)

    async def deliver(self, text, *, recovery=False):
        def current_focus():
            return self.recovery_focus if recovery else self.focus

        focus = current_focus()
        revision = self.input_revision if recovery else self.take_input_revision
        return await self.paste_to(text, focus, lambda: current_focus() is focus, self.terminal, revision,
                                   raw=recovery and self.last_raw)

    def prepare_delivery(self, *, raw=False):
        focus, revision, terminal = self.focus, self.focus_revision, self.terminal
        input_revision = self.take_input_revision

        async def deliver(text):
            return await self.paste_to(text, focus, lambda: self.focus_revision == revision, terminal, input_revision, raw=raw)

        return deliver

    async def paste_to(self, text, focus, allowed, terminal, revision, *, raw=False):
        channel = self.activity_channel
        can_continue = not raw and channel is not None and self.input_revision == revision
        previous, self.last_insertion = self.last_insertion, None
        inserted = text
        if focus is None or not allowed():
            return COPY_LAST
        if can_continue and previous and previous[:2] == (focus, revision):
            inserted = join_dictation(previous[2], text)
        guard = (channel, str(revision)) if inserted != text else ()
        status = await self.paste_attempt(inserted, focus, allowed, terminal, guard)
        if status == PASTE_CONTINUATION_CHANGED and guard:
            inserted = text
            if not allowed():
                return COPY_LAST
            status = await self.paste_attempt(text, focus, allowed, terminal, ())
            can_continue = False
        if status == PASTE_TARGET_ELEVATED:
            return ELEVATED_COPY_LAST
        if status != 0:
            return COPY_LAST
        if can_continue and self.activity_channel == channel and self.input_revision == revision and allowed():
            self.last_insertion = (focus, revision, inserted)
        return "Paste shortcut sent."

    async def paste_attempt(self, text, focus, allowed, terminal, guard):
        payload = text.replace("\r\n", "\n").replace("\n", "\r\n").encode()
        if not payload or len(payload) > MAX_PASTE_BYTES:
            return None
        channel, revision = guard or ("", "0")
        header = PASTE_REQUEST.pack(*map(int, focus), terminal, int(revision), len(payload), channel.encode("ascii"))
        worker = self.paste_worker
        fresh = worker is None or worker.returncode is not None
        try:
            if fresh:
                worker = self.paste_worker = await self.spawn("paste-server")
        except (OSError, CaptureError):
            return None
        if not allowed():
            if fresh:
                await self.close_paste_worker()
            return None
        attempt = asyncio.create_task(self.send_text(worker, header + payload))
        try:
            return await asyncio.shield(attempt)
        except asyncio.CancelledError:
            # Let a paste in flight report delivery before shutdown closes its pipe.
            await asyncio.gather(attempt, return_exceptions=True)
            raise

    async def send_text(self, worker, request):
        try:
            async with asyncio.timeout(6):
                worker.stdin.write(request)
                await worker.stdin.drain()
                status = (await worker.stdout.readexactly(1))[0]
                if status <= PASTE_TARGET_ELEVATED:
                    return status
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            pass
        await self.close_paste_worker()
        return None

    async def close_paste_worker(self):
        worker, self.paste_worker = self.paste_worker, None
        if worker is not None:
            await stop_process(worker)

    async def disable(self):
        helper, self.listener = self.listener, None
        self.cancel_shortcut_armed = False
        self.activity_channel = None
        self.last_insertion = None
        self.gesture_accepted = False
        self.close_picker()
        if self.events is not None:
            self.events.cancel()
            await asyncio.gather(self.events, return_exceptions=True)
            self.events = None
        if helper is not None:
            await stop_process(helper)
        try:
            await self.cancel_capture(include_pending=True)
        finally:
            await self.close_paste_worker()

    async def close(self):
        await self.disable()


def picker_text(text):
    data = text.replace("\r\n", "\n").replace("\n", "\r\n").encode()
    return data if b"\0" not in data and len(data) <= MAX_PASTE_BYTES else b""
