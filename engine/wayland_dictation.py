"""Portal-approved Wayland global shortcuts for local dictation."""
import asyncio
import logging
import os

from dbus_fast import Variant
from dbus_fast.errors import DBusError
from starlette.concurrency import run_in_threadpool

from linux_capture import CaptureError
from linux_desktop_portal import PATH, DesktopPortal, PortalResponseError
from portable_dictation import HISTORY_ERRORS, PortableDictation
from wayland_paste import PASTE_FALLBACK, WaylandPaste


logger = logging.getLogger(__name__)
SHORTCUTS = "org.freedesktop.portal.GlobalShortcuts"
SHORTCUT = "toggle-dictation"


def supported():
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    return bool(os.environ.get("WAYLAND_DISPLAY") or session == "wayland")


def trigger(shortcuts):
    for key, props in shortcuts:
        if key == SHORTCUT and (description := props.get("trigger_description")) and description.signature == "s":
            return description.value[:160]
    return ""


def assigned_shortcut(result):
    shortcuts = result.get("shortcuts")
    return trigger(shortcuts.value) if shortcuts and shortcuts.signature == "a(sa{sv})" else ""


class GlobalShortcuts(DesktopPortal):
    def __init__(self, activate, change, closed):
        super().__init__(closed)
        self.activate = activate
        self.change = change
        self.held = False
        self.version = 0
        self.confirmed = False
        self.reported_shortcut = ""

    async def bind(self):
        await self.connect()
        try:
            self.version = await self.property(SHORTCUTS, "version")
        except DBusError as failure:
            raise CaptureError("This Wayland desktop doesn't provide global shortcuts. Use Start dictation in Jaro.") from failure
        await self.create_session(SHORTCUTS)
        shortcuts = [
            [SHORTCUT, {"description": Variant("s", "Start or finish Jaro dictation"),
                        "preferred_trigger": Variant("s", "CTRL+ALT+SHIFT+d")}],
        ]
        try:
            granted = await self.request(SHORTCUTS, "BindShortcuts", "oa(sa{sv})sa{sv}",
                                         [self.session, shortcuts, ""], seconds=90)
        except PortalResponseError as failure:
            # GNOME's 48.0 backend can report failure after registering an approved shortcut.
            expected = assigned_shortcut(failure.result)
            desktops = os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
            if (failure.code != 2 or "gnome" not in desktops or not self.session or not expected
                    or self.reported_shortcut != expected):
                raise
            granted = await self.request(SHORTCUTS, "ListShortcuts", "oa{sv}", [self.session])
            if assigned_shortcut(granted) != expected or self.reported_shortcut != expected:
                raise failure
        self.confirmed = self.session is not None
        return assigned_shortcut(granted) if self.confirmed else ""

    def signal(self, message):
        if not super().signal(message):
            return
        if (message.interface == SHORTCUTS and message.path == PATH and self.session is not None
                and message.body and message.body[0] == self.session):
            if message.member == "ShortcutsChanged" and message.signature == "oa(sa{sv})":
                self.reported_shortcut = trigger(message.body[1])
                if self.confirmed:
                    self.change(self.reported_shortcut)
            elif self.confirmed and message.signature == "osta{sv}" and message.body[1] == SHORTCUT:
                if message.member == "Activated" and not self.held:
                    self.held = True
                    self.activate()
                elif message.member == "Deactivated":
                    self.held = False


class WaylandDictation(PortableDictation):
    platform = "wayland"

    def __init__(self, state, transcribe, history):
        super().__init__(state, transcribe, history)
        self.portal = None
        self.approval = None
        self.monitor = None
        self.portal_lost = None
        self.keyboard = None
        self.keyboard_task = None
        self.paste_authorizing = False
        self.paste_target = None
        self.command_prefix = ""
        self.command_mode = False

    @property
    def enabled(self):
        return self.command_mode or bool(self.shortcut)

    @property
    def keyboard_allowed(self):
        return self.keyboard is not None and self.keyboard.active

    def snapshot(self):
        return {**super().snapshot(), "keyboardAllowed": self.keyboard_allowed,
                "pasteAuthorizing": self.paste_authorizing, "commandPrefix": self.command_prefix,
                "commandMode": self.command_mode}

    def enable(self):
        if self.command_mode or self.portal is not None or self.authorizing:
            return
        self.authorizing = True
        self.feedback = ""
        self.approval = asyncio.create_task(self.ask_shortcut())

    async def ask_shortcut(self):
        portal = GlobalShortcuts(self.on_shortcut, self.shortcut_changed, self.disconnected)
        self.portal = portal
        try:
            self.shortcut = await portal.bind()
            self.configurable = portal.version >= 2
            self.monitor = asyncio.create_task(self.wait_for_disconnect(portal))
            if not self.shortcut:
                self.feedback = "The desktop didn't assign a shortcut. Choose one in your desktop's shortcut settings."
                if not self.configurable:
                    await self.close_portal()
        except CaptureError as failure:
            self.feedback = str(failure)
        except TimeoutError:
            self.feedback = "The desktop shortcut request timed out."
        except (DBusError, OSError, ValueError, EOFError):
            logger.info("Could not bind the Wayland dictation shortcut.", exc_info=True)
            self.feedback = "Could not connect to the desktop shortcut service. Use Start dictation in Jaro."
        finally:
            self.authorizing = False
            if self.monitor is None:
                await self.close_portal()

    async def wait_for_disconnect(self, portal):
        try:
            await portal.bus.wait_for_disconnect()
        except (OSError, EOFError):
            pass
        self.disconnected()

    def disconnected(self):
        if self.portal_lost is None or self.portal_lost.done():
            self.portal_lost = asyncio.create_task(self.end_disconnected())

    async def end_disconnected(self):
        async with self.control:
            if self.portal is not None:
                await self.disable()
                self.feedback = "The desktop closed the dictation shortcut. Enable it again to reconnect."

    def shortcut_changed(self, description):
        self.shortcut = description
        if description and self.feedback.startswith("The desktop didn't assign"):
            self.feedback = ""

    def on_shortcut(self):
        if self.enabled:
            self.toggle_from_shortcut()

    def toggle_from_shortcut(self):
        if self.phase == "recording" and (self.action is None or self.action.done()):
            self.paste_target = self.keyboard if self.keyboard_allowed else None
        self.schedule()

    async def enable_commands(self):
        if not self.command_prefix:
            raise CaptureError("Desktop shortcut commands are unavailable.")
        if self.portal is not None:
            raise CaptureError("Disable the portal shortcut before enabling commands.")
        if self.busy or self.paste_authorizing or self.authorizing:
            raise CaptureError("Finish the current dictation action first.")
        await self.save_command_preference(True)
        self.command_mode = True
        self.feedback = ""

    async def save_command_preference(self, enabled):
        try:
            await run_in_threadpool(self.history.configure, commands=enabled)
        except HISTORY_ERRORS as failure:
            raise CaptureError("Could not save the desktop shortcut preference.") from failure

    async def restore_command_preference(self, prefix):
        self.command_prefix = prefix
        try:
            self.command_mode = await run_in_threadpool(self.history.commands_enabled)
        except HISTORY_ERRORS:
            logger.warning("Could not restore the desktop shortcut preference.", exc_info=True)

    async def run_command(self, action):
        async with self.control:
            if not self.command_mode or self.state.draining:
                raise CaptureError("Enable desktop shortcut commands in Jaro’s Dictation settings.")
            if self.paste_authorizing and action != "cancel":
                raise CaptureError("Finish the automatic paste permission request first.")
            running = self.action is not None and not self.action.done()
            if action == "cancel" or action == "stop" and running and self.phase in ("idle", "starting"):
                await self.cancel_capture()
                self.feedback = "Dictation canceled."
                return "idle"
            if action == "paste-last":
                if self.busy or running:
                    raise CaptureError("Finish the current dictation before pasting the last one.")
                if not self.keyboard_allowed:
                    raise CaptureError("Allow automatic paste in Jaro or choose Copy last.")
                text = (await run_in_threadpool(self.snapshot))["text"]
                if not text:
                    raise CaptureError("There’s no last dictation to paste.")
                await asyncio.sleep(.12)
                self.feedback = await self.keyboard.paste(text, self.terminal, lambda: self.command_mode)
                if self.feedback != "Paste shortcut sent.":
                    raise CaptureError(self.feedback)
                return "pasted"
            if action in ("start", "toggle") and not running and self.phase == "idle":
                if self.state.status != "ready" or self.state.portable_recording:
                    raise CaptureError("Dictation is unavailable while Jaro starts or records audio.")
                self.schedule()
                return "starting"
            if action in ("stop", "toggle") and not running and self.phase == "recording":
                self.toggle_from_shortcut()
                return "transcribing"
            return self.phase

    async def configure_shortcut(self):
        if not self.configurable:
            raise CaptureError("Open your desktop's settings to configure shortcuts.")
        try:
            await self.portal.call(SHORTCUTS, "ConfigureShortcuts", "osa{sv}", [self.portal.session, "", {}])
        except (DBusError, TimeoutError, OSError, EOFError) as failure:
            raise CaptureError("Could not open the desktop's shortcut settings.") from failure

    def allow_paste(self):
        if not self.enabled:
            raise CaptureError("Enable the global shortcut before enabling automatic paste.")
        if self.keyboard_allowed or self.paste_authorizing:
            return
        self.paste_authorizing = True
        self.feedback = ""
        self.keyboard_task = asyncio.create_task(self.ask_keyboard())

    async def ask_keyboard(self):
        if self.keyboard is not None:
            await self.keyboard.close()
        keyboard = WaylandPaste(self.paste_revoked)
        self.keyboard = keyboard
        try:
            await keyboard.approve()
            self.feedback = "Automatic paste is ready. Finish dictation with the global shortcut in your target field."
        except CaptureError as failure:
            self.feedback = str(failure)
        except TimeoutError:
            self.feedback = "The desktop paste permission request timed out."
        except (DBusError, OSError, ValueError, EOFError):
            logger.info("Could not open the Wayland paste session.", exc_info=True)
            self.feedback = "Automatic paste is unavailable. Use Copy last to paste."
        finally:
            self.paste_authorizing = False
            if not keyboard.active:
                await keyboard.close()
                self.keyboard = None

    def paste_revoked(self):
        self.feedback = "The desktop closed automatic paste. Use Copy last to paste."

    async def disable_paste(self):
        if self.keyboard_task is not None:
            self.keyboard_task.cancel()
            await asyncio.gather(self.keyboard_task, return_exceptions=True)
            self.keyboard_task = None
        self.paste_authorizing = False
        self.paste_target = None
        if self.keyboard is not None:
            keyboard, self.keyboard = self.keyboard, None
            await keyboard.close()

    def clear_focus(self):
        self.paste_target = None

    async def deliver(self, text):
        keyboard = self.paste_target
        shortcuts = self.portal
        if keyboard is None or (shortcuts is None and not self.command_mode):
            return PASTE_FALLBACK
        try:
            async with asyncio.timeout(.8):
                while shortcuts is not None and shortcuts.held:
                    await asyncio.sleep(.025)
        except TimeoutError:
            return PASTE_FALLBACK
        await asyncio.sleep(.12 if shortcuts is None else .06)
        return await keyboard.paste(text, self.terminal, lambda: (self.paste_target is keyboard
                                                                 and (shortcuts is None or not shortcuts.held)))

    async def close_portal(self):
        if self.monitor is not None:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
            self.monitor = None
        if self.portal is not None:
            portal, self.portal = self.portal, None
            await portal.close()
        self.shortcut = ""
        self.configurable = False

    async def disable(self):
        self.command_mode = False
        if self.approval is not None:
            self.approval.cancel()
            await asyncio.gather(self.approval, return_exceptions=True)
            self.approval = None
        self.authorizing = False
        await self.cancel_capture()
        await self.disable_paste()
        await self.close_portal()

    async def close(self):
        if self.portal_lost is not None:
            self.portal_lost.cancel()
            await asyncio.gather(self.portal_lost, return_exceptions=True)
        await self.disable()
