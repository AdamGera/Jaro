"""Native X11 shortcut and visible controls for recordings launched from it."""
import asyncio
from contextlib import suppress

from Xlib import X, display, error
from starlette.concurrency import run_in_threadpool

from portable_capture import CaptureError
from portable_dictation import HISTORY_ERRORS
from portable_shortcuts import shortcut_parts
from x11_dictation import X11Dictation, lock_masks, resolve_shortcut


DEFAULT_SHORTCUT = "Ctrl+Alt+Shift+S"
DISPLAY_ERRORS = (OSError, error.DisplayError, error.ConnectionClosedError, error.XError)



class X11AnnotationBinding:
    def __init__(self, shortcut, history):
        self.action = shortcut
        self.history = history
        self.control = asyncio.Lock()
        self.shortcut = DEFAULT_SHORTCUT
        self.requested = False
        self.message = ""
        self.listener = None
        self.connection = None
        self.changing = False

    def status(self):
        return {"shortcut": self.shortcut, "defaultShortcut": DEFAULT_SHORTCUT,
                "enabled": self.listener is not None and not self.listener.done(),
                "requested": self.requested, "message": self.message}

    async def restore(self):
        try:
            shortcut, enabled = await run_in_threadpool(self.history.annotation_shortcut)
            self.shortcut = shortcut_parts(shortcut or DEFAULT_SHORTCUT)[0]
            self.requested = enabled
            if enabled:
                await self.configure(store=False)
        except (*HISTORY_ERRORS, CaptureError) as error:
            self.message = str(error) if isinstance(error, CaptureError) else "Could not read the Annotate shortcut."

    async def configure(self, *, enabled=None, shortcut=None, store=True):
        async with self.control:
            assigned = shortcut_parts(shortcut or self.shortcut)[0]
            wanted = self.requested if enabled is None else enabled
            bound = self.status()["enabled"]
            if assigned == self.shortcut and wanted == bound and wanted == self.requested:
                return
            connection = None
            self.changing = True
            try:
                if wanted:
                    connection = display.Display()
                    mapping = connection.get_modifier_mapping()
                    locks = lock_masks(connection, mapping)
                    key, modifiers = resolve_shortcut(connection, assigned, mapping, locks)
                    binding = X11Dictation.grab(connection, key, modifiers, locks)
                    if not binding:
                        raise CaptureError(f"{assigned} is already in use.")
                if store:
                    await run_in_threadpool(self.history.save_annotation_shortcut, assigned, wanted)
                await self.close()
                self.requested, self.shortcut = wanted, assigned
                self.message = ""
                if wanted:
                    self.connection = connection
                    self.listener = asyncio.create_task(self.monitor(connection, binding))
                    connection = None
            except DISPLAY_ERRORS as failure:
                raise CaptureError("Could not connect the Annotate shortcut to X11.") from failure
            except HISTORY_ERRORS as failure:
                raise CaptureError("Could not save the Annotate shortcut.") from failure
            finally:
                self.changing = False
                if connection is not None:
                    with suppress(*DISPLAY_ERRORS):
                        connection.close()

    async def monitor(self, connection, binding):
        key, masks = binding
        held = False
        try:
            while True:
                while connection.pending_events():
                    event = connection.next_event()
                    if event.type == X.KeyPress and event.detail == key and (event.state & 0xff) in masks:
                        held = True
                if held:
                    bitmap = connection.query_keymap()
                    if not bitmap[key // 8] & (1 << (key % 8)):
                        held = False
                        if not self.changing:
                            self.action.tap()
                await asyncio.sleep(.03)
        except DISPLAY_ERRORS:
            self.message = "The X11 shortcut disconnected. Enable it again to reconnect."
        finally:
            with suppress(*DISPLAY_ERRORS):
                connection.close()
            if self.connection is connection:
                self.connection = None

    async def close(self):
        if self.listener is not None:
            self.listener.cancel()
            await asyncio.gather(self.listener, return_exceptions=True)
            self.listener = None
        if self.connection is not None:
            with suppress(*DISPLAY_ERRORS):
                self.connection.close()
            self.connection = None
