"""Portal-approved Wayland global shortcuts for local dictation."""
import asyncio
from contextlib import suppress
import logging
import os
from time import monotonic

from dbus_fast import Variant
from dbus_fast.errors import DBusError
from starlette.concurrency import run_in_threadpool

import gnome_shortcuts
from linux_capture import CaptureError
from linux_desktop_portal import PATH, DesktopPortal, PortalResponseError
from linux_dictation import LinuxDictation
from portable_dictation import HISTORY_ERRORS, PASTE_SENT, QUEUE_FULL, join_dictation
from wayland_dictation_commands import GNOME_ACTIONS
from wayland_field import WaylandField
from wayland_paste import PASTE_FALLBACK, WaylandPaste


logger = logging.getLogger(__name__)
SHORTCUTS = "org.freedesktop.portal.GlobalShortcuts"
SHORTCUT = "toggle-dictation"
PASTE_LAST = "paste-last"
CANCEL = "cancel-dictation"
SHORTCUT_DEFINITIONS = (
    (SHORTCUT, "Start or finish Jaro dictation", "CTRL+ALT+SHIFT+d"),
    (PASTE_LAST, "Paste last dictation", "CTRL+ALT+v"),
    (CANCEL, "Cancel Jaro dictation", "CTRL+ALT+SHIFT+x"),
)


def supported():
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    return bool(os.environ.get("WAYLAND_DISPLAY") or session == "wayland")


def shortcut_labels(shortcuts):
    labels = {}
    for key, props in shortcuts:
        if key not in (SHORTCUT, PASTE_LAST, CANCEL):
            continue
        description = props.get("trigger_description")
        if description and description.signature == "s" and description.value.strip():
            labels[key] = description.value[:160]
    return labels


def assigned_shortcuts(result):
    shortcuts = result.get("shortcuts")
    return shortcut_labels(shortcuts.value) if shortcuts and shortcuts.signature == "a(sa{sv})" else {}


class GlobalShortcuts(DesktopPortal):
    def __init__(self, edge, change, closed):
        super().__init__(closed)
        self.edge = edge
        self.change = change
        self.held = set()
        self.version = 0
        self.confirmed = False
        self.assigned = {}

    async def bind(self, *, restoring=False):
        await self.connect()
        try:
            self.version = await self.property(SHORTCUTS, "version")
        except DBusError as failure:
            raise CaptureError("This Wayland desktop doesn't provide global shortcuts. Use Start dictation in Jaro.") from failure
        await self.create_session(SHORTCUTS)
        definitions = SHORTCUT_DEFINITIONS
        if restoring:
            previous = assigned_shortcuts(await self.request(SHORTCUTS, "ListShortcuts", "oa{sv}", [self.session]))
            if SHORTCUT not in previous:
                raise CaptureError("The desktop has no saved dictation shortcut. Choose Enable shortcut to approve it again.")
            definitions = tuple(definition for definition in definitions if definition[0] in previous)
        shortcuts = []
        for key, label, trigger in definitions:
            options = {"description": Variant("s", label)}
            if not restoring:
                options["preferred_trigger"] = Variant("s", trigger)
            shortcuts.append([key, options])
        try:
            result = await self.request(SHORTCUTS, "BindShortcuts", "oa(sa{sv})sa{sv}",
                                        [self.session, shortcuts, ""], seconds=90)
            granted = assigned_shortcuts(result)
        except PortalResponseError as failure:
            # GNOME's 48.0 backend can report failure after registering an approved shortcut.
            expected = assigned_shortcuts(failure.result)
            primary = expected.get(SHORTCUT)
            desktops = os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
            if (failure.code != 2 or "gnome" not in desktops or not self.session or not primary
                    or self.assigned.get(SHORTCUT) != primary):
                raise
            listed = assigned_shortcuts(await self.request(SHORTCUTS, "ListShortcuts", "oa{sv}", [self.session]))
            granted = {key: label for key, label in listed.items()
                       if expected.get(key) == label == self.assigned.get(key)}
            if SHORTCUT not in granted:
                raise failure
        self.confirmed = self.session is not None
        self.assigned = granted if self.confirmed else {}
        return self.assigned

    def signal(self, message):
        if not super().signal(message):
            return
        if (message.interface == SHORTCUTS and message.path == PATH and self.session is not None
                and message.body and message.body[0] == self.session):
            if message.member == "ShortcutsChanged" and message.signature == "oa(sa{sv})":
                updated = shortcut_labels(message.body[1])
                self.held.intersection_update(key for key in updated if self.assigned.get(key) == updated[key])
                self.assigned = updated
                if self.confirmed:
                    self.change(self.assigned)
            elif self.confirmed and message.signature == "osta{sv}":
                key = message.body[1]
                if message.member == "Activated" and key in self.assigned and key not in self.held:
                    self.held.add(key)
                    self.edge(key, True)
                elif message.member == "Deactivated" and key in self.held:
                    self.held.discard(key)
                    self.edge(key, False)


class WaylandDictation(LinuxDictation):
    platform = "wayland"

    def __init__(self, state, transcribe, history):
        super().__init__(state, transcribe, history)
        self.portal = None
        self.approval = None
        self.monitor = None
        self.portal_lost = None
        self.keyboard = None
        self.keyboard_task = None
        self.paste_grant_tasks = set()
        self.paste_authorizing = False
        self.paste_target = None
        self.paste_revision = 0
        self.field = None
        self.take_field = None
        self.last_insertion = None
        self.recovery_shortcut = ""
        self.cancel_shortcut = ""
        self.recovery_release = None
        self.command_prefix = ""
        self.command_mode = False
        self.gnome = None
        self.gnome_enabled = False
        self.gnome_shortcuts_need_update = False
        self.gnome_repeat_window = 1.0
        self.gnome_presses = {}

    @property
    def enabled(self):
        return self.command_mode or bool(self.shortcut)

    @property
    def keyboard_allowed(self):
        return self.keyboard is not None and self.keyboard.active

    def snapshot(self):
        return {**super().snapshot(), "keyboardAllowed": self.keyboard_allowed,
                "pasteAuthorizing": self.paste_authorizing, "commandPrefix": self.command_prefix,
                "commandMode": self.command_mode, "recoveryShortcut": self.recovery_shortcut,
                "cancelShortcut": self.cancel_shortcut,
                "gnomeShortcutAvailable": self.gnome is not None, "gnomeShortcuts": self.gnome_enabled,
                "gnomeShortcutsNeedUpdate": self.gnome_shortcuts_need_update}

    def enable(self, *, restoring=False):
        if self.command_mode or self.portal is not None or self.authorizing:
            return
        if self.gnome_enabled:
            self.feedback = "Remove Jaro’s custom GNOME shortcuts before enabling the desktop portal."
            return
        self.authorizing = True
        self.feedback = ""
        self.approval = asyncio.create_task(self.ask_shortcut(restoring=restoring))

    async def ask_shortcut(self, *, restoring=False):
        portal = GlobalShortcuts(self.on_shortcut, self.shortcut_changed, self.disconnected)
        self.portal = portal
        try:
            self.shortcut_changed(await portal.bind(restoring=restoring))
            self.configurable = portal.version >= 2
            self.monitor = asyncio.create_task(self.wait_for_disconnect(portal))
            if self.shortcut:
                self.restore_paste()
            else:
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

    def shortcut_changed(self, assignments):
        shortcut = assignments.get(SHORTCUT, "")
        if shortcut != self.shortcut:
            self.discard_shortcut()
            self.shortcut = shortcut
        recovery = assignments.get(PASTE_LAST, "")
        if recovery != self.recovery_shortcut:
            self.invalidate_recovery()
            self.recovery_shortcut = recovery
        self.cancel_shortcut = assignments.get(CANCEL, "")
        if self.shortcut and self.feedback.startswith("The desktop didn't assign"):
            self.feedback = ""

    def on_shortcut(self, key, pressed):
        if key == SHORTCUT:
            if self.shortcut and not self.authorizing:
                self.shortcut_edge(pressed)
            return
        if key == CANCEL:
            if (pressed and self.cancel_shortcut and self.can_cancel and not self.authorizing and not self.state.draining
                    and (self.discard_task is None or self.discard_task.done())):
                self.discard_task = asyncio.create_task(self.cancel_from_shortcut(self.portal, self.folder, self.action))
            return
        if not pressed:
            if self.recovery_release is not None:
                self.recovery_release.set()
            return
        if self.busy or self.state.draining or (self.action is not None and not self.action.done()):
            return
        if not self.keyboard_allowed:
            self.feedback = "Allow automatic paste in Jaro or choose Copy last."
            return
        release = asyncio.Event()
        self.recovery_release = release
        self.phase = "pasting"
        focus = self.field.focus if self.field else None
        self.action = asyncio.create_task(self.recover_from_shortcut(self.portal, self.keyboard, release, focus))

    async def cancel_from_shortcut(self, portal, folder, action):
        async with self.control:
            same_take = self.folder is folder if folder is not None else self.action is action
            if folder is not None and self.folder is None and self.jobs and self.jobs[-1].folder is folder:
                same_take = True
            if portal is self.portal and self.cancel_shortcut and same_take and not self.state.draining:
                await self.cancel_dictation()

    def invalidate_recovery(self):
        if self.recovery_release is not None:
            release, self.recovery_release = self.recovery_release, None
            release.set()

    async def recover_from_shortcut(self, portal, keyboard, release, focus):
        def allowed():
            return (self.recovery_release is release and self.portal is portal and not portal.held
                    and self.keyboard is keyboard and self.keyboard_allowed and not self.state.draining)

        try:
            await asyncio.wait_for(release.wait(), timeout=5)
            await self.paste_last(keyboard, allowed, focus)
        except (CaptureError, TimeoutError) as failure:
            if self.recovery_release is release:
                self.feedback = str(failure) if isinstance(failure, CaptureError) else PASTE_FALLBACK
        finally:
            self.invalidate_recovery()
            self.phase = "idle"

    async def paste_last(self, keyboard, allowed, focus):
        await asyncio.sleep(.12)
        if not allowed():
            raise CaptureError(PASTE_FALLBACK)
        text = (await run_in_threadpool(self.snapshot))["text"]
        if not text:
            raise CaptureError("There’s no last dictation to paste.")
        result = await self.paste_in_field(text, keyboard, allowed, self.terminal, focus)
        if allowed():
            self.feedback = result
        if result != PASTE_SENT:
            raise CaptureError(result)

    def toggle_from_shortcut(self, *, released_at=None):
        if self.phase == "recording" and (self.action is None or self.action.done()):
            self.paste_target = self.keyboard if self.keyboard_allowed else None
        super().toggle_from_shortcut(released_at=released_at)

    async def enable_commands(self):
        if not self.command_prefix:
            raise CaptureError("Desktop shortcut commands are unavailable.")
        if self.portal is not None:
            raise CaptureError("Disable the portal shortcut before enabling commands.")
        if self.busy or self.paste_authorizing or self.authorizing:
            raise CaptureError("Finish the current dictation action first.")
        if not self.command_mode:
            await self.save_command_preference(True)
            self.command_mode = True
        self.feedback = ""

    async def add_gnome_shortcuts(self):
        if self.gnome is None:
            raise CaptureError("GNOME custom shortcuts are unavailable.")
        previous = self.command_mode
        await self.enable_commands()
        try:
            missing_key = await run_in_threadpool(self.gnome.install)
        except CaptureError:
            if not self.gnome_enabled:
                with suppress(CaptureError):
                    await run_in_threadpool(self.gnome.remove)
            if not previous:
                with suppress(CaptureError):
                    await self.save_command_preference(False)
                    self.command_mode = False
            raise
        self.gnome_enabled = True
        self.gnome_shortcuts_need_update = False
        self.gnome_repeat_window = await run_in_threadpool(gnome_shortcuts.repeat_window)
        self.feedback = ("Shortcuts added. Assign any unbound keys in GNOME Settings → Keyboard → Custom Shortcuts."
                         if missing_key else "Jaro shortcuts added to GNOME Settings.")

    async def remove_gnome_shortcuts(self):
        if self.gnome is None:
            raise CaptureError("GNOME custom shortcuts are unavailable.")
        await run_in_threadpool(self.gnome.remove)
        self.gnome_enabled = False
        self.gnome_shortcuts_need_update = False
        self.gnome_presses.clear()
        self.feedback = "Jaro shortcuts removed from GNOME Settings."

    async def disable_commands(self):
        if self.gnome_enabled:
            await self.remove_gnome_shortcuts()
        await self.save_command_preference(False)

    async def save_command_preference(self, enabled):
        try:
            await run_in_threadpool(self.history.configure, commands=enabled)
        except HISTORY_ERRORS as failure:
            raise CaptureError("Could not save the desktop shortcut preference.") from failure

    async def restore_command_preference(self, prefix, root):
        self.command_prefix = prefix
        try:
            self.command_mode = await run_in_threadpool(self.history.commands_enabled)
        except HISTORY_ERRORS:
            logger.warning("Could not restore the desktop shortcut preference.", exc_info=True)
        if gnome_shortcuts.supported():
            gnome = gnome_shortcuts.GnomeShortcuts(root)
            try:
                installed = await run_in_threadpool(gnome.installed_entries)
                self.gnome_enabled = bool(installed)
                self.gnome_shortcuts_need_update = self.gnome_enabled and installed != gnome.entries.keys()
                self.gnome = gnome
                if self.gnome_enabled:
                    self.gnome_repeat_window = await run_in_threadpool(gnome_shortcuts.repeat_window)
            except CaptureError:
                logger.info("GNOME custom shortcuts are unavailable.")

    async def run_command(self, action):
        arrival = monotonic()
        async with self.control:
            if not self.command_mode or self.state.draining:
                raise CaptureError("Enable desktop shortcut commands in Jaro’s Dictation settings.")
            if action in GNOME_ACTIONS:
                if not self.gnome_enabled:
                    raise CaptureError("Add GNOME shortcuts in Jaro’s Dictation settings.")
                action = GNOME_ACTIONS[action]
                previous = self.gnome_presses.get(action)
                self.gnome_presses[action] = arrival
                # GNOME repeats custom key presses and does not send key release.
                if previous is not None and arrival - previous <= self.gnome_repeat_window:
                    return self.phase
            if self.paste_authorizing and action != "cancel":
                raise CaptureError("Finish the automatic paste permission request first.")
            running = self.action is not None and not self.action.done()
            if action == "cancel":
                await self.cancel_dictation()
                return self.phase
            if action == "stop" and running and self.phase in ("idle", "starting"):
                await self.cancel_capture()
                self.feedback = "Dictation canceled."
                return "idle"
            if action == "paste-last":
                if self.busy or running:
                    raise CaptureError("Finish the current dictation before pasting the last one.")
                if not self.keyboard_allowed:
                    self.feedback = "Allow automatic paste in Jaro or choose Copy last."
                    raise CaptureError(self.feedback)
                keyboard = self.keyboard
                focus = self.field.focus if self.field else None
                self.phase = "pasting"
                try:
                    await self.paste_last(keyboard, lambda: self.command_mode and self.keyboard is keyboard
                                          and self.keyboard_allowed and not self.state.draining, focus)
                except CaptureError as failure:
                    self.feedback = str(failure)
                    raise
                finally:
                    self.phase = "idle"
                return "pasted"
            if action in ("start", "toggle") and self.can_start_recording:
                if self.state.status != "ready" or self.state.portable_recording:
                    raise CaptureError("Dictation is unavailable while Jaro starts or records audio.")
                self.schedule()
                return "starting"
            if action in ("stop", "toggle") and not running and self.phase == "recording":
                self.toggle_from_shortcut()
                return "transcribing"
            if action in ("start", "toggle") and self.queue_full:
                raise CaptureError(QUEUE_FULL)
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
        self.start_paste()

    def restore_paste(self):
        if self.enabled and not self.state.draining:
            self.start_paste(restoring=True)

    def start_paste(self, *, restoring=False):
        if self.keyboard_allowed or self.paste_authorizing:
            return
        self.paste_authorizing = True
        if not restoring:
            self.feedback = ""
        self.keyboard_task = asyncio.create_task(self.ask_keyboard(restoring=restoring))

    async def ask_keyboard(self, *, restoring=False):
        keyboard = None
        try:
            try:
                token = await self.update_paste_grant(self.history.consume_paste_grant)
            except HISTORY_ERRORS:
                logger.warning("Could not read the saved automatic paste grant.", exc_info=True)
                token = ""
            if not isinstance(token, str) or len(token) > 1024 or "\0" in token:
                token = ""
            if restoring and not token:
                return
            if self.keyboard is not None:
                previous, self.keyboard = self.keyboard, None
                await previous.close()
            keyboard = self.keyboard = WaylandPaste(self.paste_revoked)
            await keyboard.approve(token)
            remembered = await self.save_keyboard_grant(keyboard) if keyboard.restore_token else False
            if keyboard.active:
                field = WaylandField()
                try:
                    await field.start()
                    if keyboard.active and field.bus is not None:
                        self.field = field
                finally:
                    if self.field is not field:
                        field.close()
            if keyboard.active:
                self.feedback = ("Automatic paste is ready." if remembered else
                                 "Automatic paste is ready for this launch. Your desktop may ask again after restart.")
        except CaptureError as failure:
            self.feedback = str(failure)
        except TimeoutError:
            self.feedback = "The desktop paste permission request timed out."
        except (DBusError, OSError, ValueError, EOFError):
            logger.info("Could not open the Wayland paste session.", exc_info=True)
            self.feedback = "Automatic paste is unavailable. Use Copy last to paste."
        finally:
            try:
                if keyboard is not None and not keyboard.active:
                    self.keyboard = None
                    await keyboard.close()
            finally:
                self.paste_authorizing = False

    @staticmethod
    async def update_paste_grant(operation, *args):
        write = asyncio.create_task(run_in_threadpool(operation, *args))
        try:
            return await asyncio.shield(write)
        except asyncio.CancelledError:
            # Wait for the database write before a later settings action can erase the grant.
            await asyncio.gather(write, return_exceptions=True)
            raise

    async def save_keyboard_grant(self, keyboard):
        token = keyboard.restore_token
        try:
            await self.update_paste_grant(self.history.save_paste_grant, token)
        except HISTORY_ERRORS:
            logger.warning("Could not remember automatic paste.", exc_info=True)
            return False
        finally:
            if not keyboard.active:
                await self.forget_revoked_paste(token)
        return True

    def paste_revoked(self):
        self.invalidate_recovery()
        self.paste_target = None
        self.close_field()
        self.feedback = "The desktop closed automatic paste. Use Copy last to paste."
        if self.keyboard is not None and self.keyboard.restore_token:
            task = asyncio.create_task(self.forget_revoked_paste(self.keyboard.restore_token))
            self.paste_grant_tasks.add(task)
            task.add_done_callback(self.paste_grant_tasks.discard)

    async def forget_revoked_paste(self, token):
        try:
            await run_in_threadpool(self.history.forget_paste_grant, token)
        except HISTORY_ERRORS:
            logger.warning("Could not remove the expired automatic paste grant.", exc_info=True)

    async def disable_paste(self, *, forget=True):
        self.invalidate_recovery()
        if self.keyboard_task is not None:
            self.keyboard_task.cancel()
            await asyncio.gather(self.keyboard_task, return_exceptions=True)
            self.keyboard_task = None
        self.paste_authorizing = False
        self.paste_target = None
        self.close_field()
        if self.keyboard is not None:
            keyboard, self.keyboard = self.keyboard, None
            await keyboard.close()
        if forget:
            try:
                await run_in_threadpool(self.history.save_paste_grant, "")
            except HISTORY_ERRORS as failure:
                raise CaptureError("Could not remove the saved automatic paste grant.") from failure

    def clear_focus(self):
        self.paste_target = None
        self.take_field = None
        self.paste_revision += 1

    def watch_focus(self):
        # The portal alone cannot confirm which field an earlier take targeted.
        self.clear_focus()
        if self.field:
            self.take_field = self.field.focus

    def prepare_delivery(self):
        keyboard, shortcuts, revision, terminal = self.paste_target, self.portal, self.paste_revision, self.terminal
        field, focus = self.field, self.take_field

        def superseded():
            return self.paste_revision != revision

        def allowed():
            same_field = focus is not None and field is not None and field is self.field and field.focus == focus
            return ((not superseded() or same_field) and self.keyboard is keyboard and self.portal is shortcuts
                    and (shortcuts is not None or self.command_mode))

        async def deliver(text):
            return await self.paste_to(text, keyboard, shortcuts, allowed, terminal, focus, superseded)

        return deliver

    async def deliver(self, text):
        return await self.prepare_delivery()(text)

    async def paste_to(self, text, keyboard, shortcuts, allowed, terminal, focus, superseded):
        if keyboard is None or not allowed():
            self.last_insertion = None
            return PASTE_FALLBACK
        try:
            async with asyncio.timeout(.8):
                while shortcuts is not None and shortcuts.held:
                    await asyncio.sleep(.025)
        except TimeoutError:
            self.last_insertion = None
            return PASTE_FALLBACK
        await asyncio.sleep(.12 if shortcuts is None else .06)
        return await self.paste_in_field(text, keyboard, lambda: allowed() and (shortcuts is None or not shortcuts.held),
                                         terminal, focus, superseded)

    def close_field(self):
        if self.field is not None:
            self.field.close()
            self.field = None
        self.last_insertion = None

    async def copy_interrupted_text(self, text):
        self.last_insertion = None
        return await super().copy_interrupted_text(text)

    async def paste_in_field(self, text, keyboard, allowed, terminal, focus, superseded=None):
        field = self.field
        previous, self.last_insertion = self.last_insertion, None

        def same_destination():
            return allowed() and (focus is None or (field is not None and field is self.field and field.focus == focus))

        if not same_destination():
            return PASTE_FALLBACK
        before = await field.snapshot() if focus is not None else None
        if not same_destination():
            return PASTE_FALLBACK
        if before is None or before.focus != focus:
            if superseded is not None and superseded():
                return PASTE_FALLBACK
            return await keyboard.paste(text, terminal, same_destination)
        inserted = join_dictation(previous[1], text) if previous and previous[0] == before else text
        blocked = False

        def can_join():
            nonlocal blocked
            valid = same_destination()
            blocked = valid and not field.matches(before)
            return valid and not blocked

        result = await keyboard.paste(inserted, terminal, can_join if inserted != text else same_destination)
        if result != PASTE_SENT:
            if blocked and same_destination():
                return await keyboard.paste(text, terminal, same_destination)
            return result
        after = await field.snapshot()
        if (after is not None and after.focus == before.focus and after.revision != before.revision
                and after.caret == before.caret + len(inserted) and after.length == before.length + len(inserted)):
            self.last_insertion = (after, inserted)
        return result

    async def close_portal(self):
        if self.monitor is not None:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
            self.monitor = None
        if self.portal is not None:
            portal, self.portal = self.portal, None
            await portal.close()
        self.shortcut_changed({})
        self.configurable = False

    async def disable(self, *, forget_paste=False):
        self.command_mode = False
        self.gnome_presses.clear()
        if self.approval is not None:
            self.approval.cancel()
            await asyncio.gather(self.approval, return_exceptions=True)
            self.approval = None
        self.authorizing = False
        await self.cancel_capture(include_pending=True)
        try:
            await self.disable_paste(forget=forget_paste)
        finally:
            await self.close_portal()

    async def close(self):
        if self.portal_lost is not None:
            self.portal_lost.cancel()
            await asyncio.gather(self.portal_lost, return_exceptions=True)
        await self.disable()
        if self.paste_grant_tasks:
            await asyncio.gather(*self.paste_grant_tasks)
