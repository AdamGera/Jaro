"""An independent Windows global hotkey for starting, drawing, and saving."""
import asyncio

from starlette.concurrency import run_in_threadpool

from portable_capture import CaptureError
from portable_dictation import HISTORY_ERRORS
from windows_dictation import stop_process, windows_shortcut_parts


DEFAULT_SHORTCUT = "Ctrl+Alt+Shift+F10"


def binding_parts(value):
    label, chord = windows_shortcut_parts(value)
    if not chord or chord[0] == str(0x7B):  # F12 is reserved for the debugger.
        raise CaptureError("Choose a chord other than Left Alt or F12 for Annotate.")
    return label, chord


class WindowsAnnotationBinding:
    def __init__(self, action, dictation):
        self.action = action
        self.dictation = dictation
        self.shortcut = DEFAULT_SHORTCUT
        self.requested = False
        self.message = ""
        self.helper = None
        self.listener = None
        self.changing = False

    def status(self):
        return {"shortcut": self.shortcut, "defaultShortcut": DEFAULT_SHORTCUT,
                "requested": self.requested, "enabled": self.helper is not None, "message": self.message}

    def check_dictation(self, value):
        if self.requested and windows_shortcut_parts(value)[1] == binding_parts(self.shortcut)[1]:
            raise CaptureError("This shortcut is already used for Annotate.")

    async def restore(self):
        try:
            saved, self.requested = await run_in_threadpool(self.dictation.history.annotation_shortcut, windows=True)
            self.shortcut = binding_parts(saved or DEFAULT_SHORTCUT)[0]
            if self.requested:
                await self.configure(store=False)
        except CaptureError as failure:
            self.message = str(failure)
        except HISTORY_ERRORS:
            self.message = "Could not read the Annotate shortcut."

    async def configure(self, *, enabled=None, shortcut=None, store=True):
        label, chord = binding_parts(shortcut or self.shortcut)
        wanted = self.requested if enabled is None else enabled
        if wanted and chord == windows_shortcut_parts(self.dictation.shortcut)[1]:
            raise CaptureError("This shortcut is already used for Dictation.")
        raw_shortcut = self.dictation.raw_shortcut
        if wanted and raw_shortcut and chord == windows_shortcut_parts(raw_shortcut)[1]:
            raise CaptureError("This shortcut is already used for dictation without processing.")
        if label == self.shortcut and wanted == self.requested and wanted == (self.helper is not None):
            return
        candidate = None
        self.changing = True
        try:
            if wanted:
                candidate = await self.dictation.spawn("annotate", *chord)
                async with asyncio.timeout(4):
                    ready = (await candidate.stdout.readline()).strip()
                if ready == b"conflict":
                    raise CaptureError(f"{label} is already registered in Windows.")
                if ready != b"ready":
                    raise CaptureError("Windows could not register the Annotate shortcut.")
            if store:
                await run_in_threadpool(self.dictation.history.save_annotation_shortcut, label, wanted, windows=True)
            await self.close()
            self.shortcut, self.requested, self.message = label, wanted, ""
            if candidate is not None:
                self.helper = candidate
                self.listener = asyncio.create_task(self.monitor(candidate))
                candidate = None
        except HISTORY_ERRORS as failure:
            raise CaptureError("Could not change the Windows Annotate shortcut.") from failure
        finally:
            self.changing = False
            if candidate is not None:
                await stop_process(candidate)

    async def monitor(self, helper):
        try:
            while event := await helper.stdout.readline():
                if self.changing:
                    continue
                event = event.strip()
                if event == b"down":
                    if self.action.state.portable_screencast is None:
                        self.action.tap()
                    else:
                        self.action.edge(True)
                elif event == b"up":
                    self.action.edge(False)
                elif event == b"cancel":
                    self.action.release(cancel=True)
        except (OSError, ValueError):
            pass
        finally:
            if self.helper is helper:
                self.action.release(cancel=True)
                try:
                    await stop_process(helper)
                finally:
                    if self.helper is helper:
                        self.helper = None
                        self.message = "The Windows Annotate shortcut disconnected. Enable it again to reconnect."

    async def close(self):
        helper, self.helper = self.helper, None
        if self.listener is not None:
            self.listener.cancel()
            await asyncio.gather(self.listener, return_exceptions=True)
            self.listener = None
        if helper is not None:
            self.action.release(cancel=True)
            await stop_process(helper)
