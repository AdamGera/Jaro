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
from linux_desktop_portal import APP_ID, DBUS, PATH, DesktopPortal, PortalResponseError
from linux_dictation import LinuxDictation
from portable_dictation import HISTORY_ERRORS, OutputContext, PASTE_SENT, join_dictation
from wayland_dictation_commands import GNOME_ACTIONS
from wayland_field import ERRORS as FOCUS_ERRORS, SwayFocus, WaylandField, output_window
from wayland_paste import PASTE_FALLBACK, WaylandPaste


logger = logging.getLogger(__name__)
SHORTCUTS = "org.freedesktop.portal.GlobalShortcuts"
SHORTCUT = "toggle-dictation"
RAW = "raw-dictation"
PASTE_LAST = "paste-last"
CANCEL = "cancel-dictation"
ANNOTATE = "annotate"
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
        if key not in (SHORTCUT, RAW, PASTE_LAST, CANCEL, ANNOTATE):
            continue
        description = props.get("trigger_description")
        if description and description.signature == "s" and description.value.strip():
            labels[key] = description.value[:160]
    return labels


def assigned_shortcuts(result):
    shortcuts = result.get("shortcuts")
    return shortcut_labels(shortcuts.value) if shortcuts and shortcuts.signature == "a(sa{sv})" else {}


class GlobalShortcuts(DesktopPortal):
    def __init__(self, edge, change, closed, *, annotation=False, raw=False):
        super().__init__(closed)
        self.edge = edge
        self.change = change
        self.held = set()
        self.version = 0
        self.confirmed = False
        self.assigned = {}
        self.definitions = SHORTCUT_DEFINITIONS
        if annotation:
            self.definitions += ((ANNOTATE, "Record, save, or draw on the screen with Jaro", "CTRL+ALT+SHIFT+s"),)
        if raw:
            self.definitions += ((RAW, "Dictate without processing", "CTRL+ALT+SHIFT+j"),)
        self.requested = {key for key, _, _ in self.definitions}

    async def bind(self, *, restoring=False, add_raw=False, active_assignments=None):
        await self.connect()
        try:
            self.version = await self.property(SHORTCUTS, "version")
        except DBusError as failure:
            raise CaptureError("This Wayland desktop doesn't provide global shortcuts. Use Start dictation in Jaro.") from failure
        await self.create_session(SHORTCUTS)
        definitions = self.definitions
        primary_keys = self.requested & {SHORTCUT, RAW, ANNOTATE}
        previous = set()
        desktops = os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
        if restoring:
            previous = set(assigned_shortcuts(await self.request(SHORTCUTS, "ListShortcuts", "oa{sv}", [self.session])))
            if not previous and "gnome" in desktops:
                # GNOME 48 only lists assignments already bound to this session.
                saved = await run_in_threadpool(gnome_shortcuts.portal_shortcut_ids, APP_ID)
                previous = saved if saved is not None else (active_assignments or set())
            if primary_keys.isdisjoint(previous):
                raise CaptureError("The desktop has no saved dictation or annotation shortcut. Choose Enable shortcut to approve it again.")
            definitions = tuple(definition for definition in definitions
                                if definition[0] in previous or (add_raw and definition[0] == RAW))
        self.requested = {key for key, _, _ in definitions}
        shortcuts = []
        for key, label, trigger in definitions:
            options = {"description": Variant("s", label)}
            if not restoring or key not in previous:
                options["preferred_trigger"] = Variant("s", trigger)
            shortcuts.append([key, options])
        try:
            result = await self.request(SHORTCUTS, "BindShortcuts", "oa(sa{sv})sa{sv}",
                                        [self.session, shortcuts, ""], seconds=90)
            granted = assigned_shortcuts(result)
        except PortalResponseError as failure:
            # GNOME's 48.0 backend can report failure after registering an approved shortcut.
            expected = assigned_shortcuts(failure.result)
            key = next((key for key in (SHORTCUT, RAW, ANNOTATE) if key in expected and key in primary_keys), SHORTCUT)
            primary = expected.get(key)
            if (failure.code != 2 or "gnome" not in desktops or not self.session or not primary
                    or self.assigned.get(key) != primary):
                raise
            listed = assigned_shortcuts(await self.request(SHORTCUTS, "ListShortcuts", "oa{sv}", [self.session]))
            granted = {key: label for key, label in listed.items()
                       if expected.get(key) == label == self.assigned.get(key)}
            if key not in granted:
                raise failure
        self.confirmed = self.session is not None
        self.assigned = {key: label for key, label in granted.items() if key in self.requested} if self.confirmed else {}
        return self.assigned

    def signal(self, message):
        if not super().signal(message):
            return
        if (message.interface == SHORTCUTS and message.path == PATH and self.session is not None
                and message.body and message.body[0] == self.session):
            if message.member == "ShortcutsChanged" and message.signature == "oa(sa{sv})":
                updated = {key: label for key, label in shortcut_labels(message.body[1]).items() if key in self.requested}
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
        self.saved_virtual_paste = False
        self.paste_target = None
        self.paste_revision = 0
        self.field = None
        self.take_field = None
        self.sway = None
        self.take_window = None
        self.last_insertion = None
        self.recovery_shortcut = ""
        self.raw_shortcut = ""
        self.raw_shortcut_requested = False
        try:
            self.raw_shortcut_requested = history.portal_raw_shortcut()
        except HISTORY_ERRORS:
            logger.warning("Could not read the Wayland raw dictation shortcut preference.", exc_info=True)
        self.cancel_shortcut = ""
        self.annotation_shortcut = ""
        self.recovery_release = None
        self.command_prefix = ""
        self.command_mode = False
        self.gnome = None
        self.gnome_enabled = False
        self.gnome_annotation = False
        self.gnome_shortcuts_need_update = False
        self.gnome_repeat_window = 1.0
        self.gnome_presses = {}

    @property
    def enabled(self):
        return self.command_mode or bool(self.shortcut or self.raw_shortcut)

    @property
    def keyboard_allowed(self):
        return self.keyboard is not None and self.keyboard.active

    def snapshot(self):
        return {**super().snapshot(), "keyboardAllowed": self.keyboard_allowed,
                "pasteAuthorizing": self.paste_authorizing, "savedVirtualPaste": self.saved_virtual_paste,
                "commandPrefix": self.command_prefix,
                "commandMode": self.command_mode, "recoveryShortcut": self.recovery_shortcut,
                "portalRawShortcut": self.raw_shortcut, "portalRawRequested": self.raw_shortcut_requested,
                "cancelShortcut": self.cancel_shortcut, "annotationShortcut": self.annotation_shortcut,
                "gnomeShortcutAvailable": self.gnome is not None, "gnomeShortcuts": self.gnome_enabled,
                "gnomeAnnotation": self.gnome_annotation,
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

    async def ask_shortcut(self, *, restoring=False, active_assignments=None):
        if await self.bind_shortcuts(restoring=restoring, add_raw=active_assignments is not None and self.raw_shortcut_requested,
                                     active_assignments=active_assignments):
            return
        # A cancelled change should recover the saved shortcuts without another assignment prompt.
        if active_assignments is not None:
            feedback = self.feedback
            if await self.bind_shortcuts(restoring=True, active_assignments=active_assignments):
                self.feedback = feedback

    async def bind_shortcuts(self, *, restoring, add_raw=False, active_assignments=None):
        self.authorizing = True
        portal = GlobalShortcuts(self.on_shortcut, self.shortcut_changed, self.disconnected,
                                 annotation=bool(getattr(self.state, "portable_annotation_shortcut", None)),
                                 raw=self.raw_shortcut_requested)
        self.portal = portal
        try:
            self.shortcut_changed(await portal.bind(restoring=restoring, add_raw=add_raw, active_assignments=active_assignments))
            self.configurable = portal.version >= 2
            self.monitor = asyncio.create_task(self.wait_for_disconnect(portal))
            if self.enabled and not self.keyboard_allowed:
                self.restore_paste()
            elif not self.enabled and not self.annotation_shortcut:
                self.feedback = "The desktop didn't assign a shortcut. Choose one in your desktop's shortcut settings."
                if not self.configurable:
                    await self.close_portal()
            return self.monitor is not None
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
        return False

    async def change_raw_shortcut(self, enabled):
        if (self.busy or self.action_pending() or self.authorizing or self.paste_authorizing
                or self.pressed_at is not None or self.tap_timer is not None or (self.portal and self.portal.held)):
            raise CaptureError("Finish the current dictation before changing the shortcut.")
        if self.raw_shortcut_requested == enabled and (not enabled or self.raw_shortcut):
            return
        self.authorizing = True
        restarting = False
        try:
            await run_in_threadpool(self.history.save_portal_raw_shortcut, enabled)
            self.raw_shortcut_requested = enabled
            if self.portal is not None:
                active_assignments = set(self.portal.assigned)
                await self.close_portal()
                self.feedback = ""
                self.approval = asyncio.create_task(self.ask_shortcut(restoring=True, active_assignments=active_assignments))
                restarting = True
        except HISTORY_ERRORS as failure:
            raise CaptureError("Could not save the raw dictation shortcut preference.") from failure
        finally:
            if not restarting:
                self.authorizing = False

    async def wait_for_disconnect(self, portal):
        try:
            await portal.bus.wait_for_disconnect()
        except (OSError, EOFError):
            pass
        self.disconnected()

    def disconnected(self):
        if annotation := getattr(self.state, "portable_annotation_shortcut", None):
            annotation.release(cancel=True)
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
            self.last_tap.pop(False, None)
            if self.shortcut_owner is not True and not (self.pressed_at is not None and self.pressed_raw):
                self.discard_shortcut()
            self.shortcut = shortcut
        label = assignments.get(RAW, "") if self.raw_shortcut_requested else ""
        if label and any(label.casefold() == assignments.get(key, "").casefold()
                         for key in (SHORTCUT, PASTE_LAST, CANCEL, ANNOTATE)):
            label = ""
            self.feedback = "Choose a different desktop shortcut for dictation without processing."
        if label != self.raw_shortcut:
            self.last_tap.pop(True, None)
            if self.shortcut_owner is True or (self.pressed_at is not None and self.pressed_raw):
                self.discard_shortcut()
            self.raw_shortcut = label
        recovery = assignments.get(PASTE_LAST, "")
        if recovery != self.recovery_shortcut:
            self.invalidate_recovery()
            self.recovery_shortcut = recovery
        self.cancel_shortcut = assignments.get(CANCEL, "")
        annotation = getattr(self.state, "portable_annotation_shortcut", None)
        label = assignments.get(ANNOTATE, "") if annotation else ""
        if annotation and self.annotation_shortcut != label:
            annotation.release(cancel=True)
        self.annotation_shortcut = label
        if self.shortcut and self.feedback.startswith("The desktop didn't assign"):
            self.feedback = ""

    def on_shortcut(self, key, pressed):
        if key == ANNOTATE:
            if self.annotation_shortcut and not self.authorizing:
                self.state.portable_annotation_shortcut.edge(pressed)
            return
        if key == SHORTCUT:
            if self.shortcut and not self.authorizing:
                self.shortcut_edge(pressed)
            return
        if key == RAW:
            if self.raw_shortcut and not self.authorizing:
                self.shortcut_edge(pressed, raw=True)
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
        window = self.sway.focus if self.sway else None
        self.action = asyncio.create_task(self.recover_from_shortcut(self.portal, self.keyboard, release, focus, window))

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

    async def recover_from_shortcut(self, portal, keyboard, release, focus, window):
        def allowed():
            return (self.recovery_release is release and self.portal is portal and not portal.held
                    and self.keyboard is keyboard and self.keyboard_allowed and not self.state.draining)

        try:
            await asyncio.wait_for(release.wait(), timeout=5)
            await self.paste_last(keyboard, allowed, focus, window)
        except (CaptureError, TimeoutError) as failure:
            if self.recovery_release is release:
                self.feedback = str(failure) if isinstance(failure, CaptureError) else PASTE_FALLBACK
        finally:
            self.invalidate_recovery()
            self.phase = "idle"

    async def paste_last(self, keyboard, allowed, focus, window=None):
        await asyncio.sleep(.12)
        if not allowed():
            raise CaptureError(PASTE_FALLBACK)
        text = (await run_in_threadpool(self.snapshot))["text"]
        if not text:
            raise CaptureError("There’s no last dictation to paste.")
        result = await self.paste_in_field(text, keyboard, allowed, self.terminal, focus, window=window, raw=self.last_raw)
        if allowed():
            self.feedback = result
        if result != PASTE_SENT:
            raise CaptureError(result)

    def toggle_from_shortcut(self, *, released_at=None, raw=False):
        if self.phase == "recording" and (self.action is None or self.action.done()):
            self.paste_target = self.keyboard if self.keyboard_allowed else None
        return super().toggle_from_shortcut(released_at=released_at, raw=raw)

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
        self.gnome_annotation = bool(getattr(self.state, "portable_annotation_shortcut", None))
        self.gnome_shortcuts_need_update = False
        self.gnome_repeat_window = await run_in_threadpool(gnome_shortcuts.repeat_window)
        self.feedback = ("Shortcuts added. Assign any unbound keys in GNOME Settings → Keyboard → Custom Shortcuts."
                         if missing_key else "Jaro shortcuts added to GNOME Settings.")

    async def remove_gnome_shortcuts(self):
        if self.gnome is None:
            raise CaptureError("GNOME custom shortcuts are unavailable.")
        await run_in_threadpool(self.gnome.remove)
        self.gnome_enabled = False
        self.gnome_annotation = False
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
            annotation = bool(getattr(self.state, "portable_annotation_shortcut", None))
            gnome = gnome_shortcuts.GnomeShortcuts(root, annotation=annotation)
            try:
                installed = await run_in_threadpool(gnome.installed_entries)
                self.gnome_enabled = bool(installed)
                self.gnome_annotation = annotation and gnome.annotation_entry in installed
                self.gnome_shortcuts_need_update = self.gnome_enabled and installed != gnome.entries.keys()
                self.gnome = gnome
                if self.gnome_enabled:
                    self.gnome_repeat_window = await run_in_threadpool(gnome_shortcuts.repeat_window)
            except CaptureError:
                logger.info("GNOME custom shortcuts are unavailable.")

    async def perform_command(self, action, arrival):
        if action in GNOME_ACTIONS:
            if not self.command_mode:
                raise CaptureError("Enable desktop shortcut commands in Jaro’s Dictation settings.")
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
        if action == "annotate":
            annotation = getattr(self.state, "portable_annotation_shortcut", None)
            if not (self.gnome_annotation and annotation):
                raise CaptureError("Update Jaro’s GNOME shortcuts to enable Annotate.")
            return "queued" if annotation.tap() else "busy"
        return await super().perform_command(action, arrival)

    async def command_paste_last(self):
        if not self.keyboard_allowed:
            self.feedback = "Allow automatic paste in Jaro or choose Copy last."
            raise CaptureError(self.feedback)
        keyboard = self.keyboard
        focus = self.field.focus if self.field else None
        window = self.sway.focus if self.sway else None
        self.phase = "pasting"
        try:
            await self.paste_last(keyboard, lambda: self.enabled and self.keyboard is keyboard
                                  and self.keyboard_allowed and not self.state.draining, focus, window)
        except CaptureError as failure:
            self.feedback = str(failure)
            raise
        finally:
            self.phase = "idle"

    async def configure_shortcut(self):
        if not self.configurable:
            raise CaptureError("Open your desktop's settings to configure shortcuts.")
        try:
            await self.portal.call(SHORTCUTS, "ConfigureShortcuts", "osa{sv}", [self.portal.session, "", {}])
        except (DBusError, TimeoutError, OSError, EOFError) as failure:
            if isinstance(failure, DBusError) and failure.type in (
                DBUS + ".Error.UnknownMethod", DBUS + ".Error.UnknownInterface", DBUS + ".Error.NotSupported",
            ):
                # A portal can advertise this method even if its desktop backend lacks it.
                self.configurable = False
                if not (self.enabled or self.annotation_shortcut):
                    await self.close_portal()
                self.feedback = "Open your desktop's settings to configure shortcuts."
                raise CaptureError(self.feedback) from failure
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
                token, self.saved_virtual_paste = await self.update_paste_grant(self.history.consume_paste_grant)
            except HISTORY_ERRORS:
                logger.warning("Could not read the saved automatic paste grant.", exc_info=True)
                if restoring:
                    return
                token = ""
            if not isinstance(token, str) or len(token) > 1024 or "\0" in token:
                token = ""
            virtual_only = restoring and self.saved_virtual_paste and not token
            if restoring and not (token or virtual_only):
                return
            if self.keyboard is not None:
                previous, self.keyboard = self.keyboard, None
                await previous.close()
            keyboard = self.keyboard = WaylandPaste(self.paste_revoked)
            await keyboard.approve(token, virtual_only=virtual_only)
            remembered = virtual_only or await self.save_keyboard_grant(keyboard)
            if keyboard.active:
                if socket := os.environ.get("SWAYSOCK"):
                    self.sway = SwayFocus()
                    await self.sway.start(socket)
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
        virtual = bool(keyboard.wtype)
        if not (token or virtual or self.saved_virtual_paste):
            return False
        try:
            await self.update_paste_grant(self.history.save_paste_grant, token, virtual)
            self.saved_virtual_paste = virtual
        except HISTORY_ERRORS:
            logger.warning("Could not remember automatic paste.", exc_info=True)
            return False
        finally:
            if token and not keyboard.active:
                await self.forget_revoked_paste(token)
        return bool(token or virtual)

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
                self.saved_virtual_paste = False
            except HISTORY_ERRORS as failure:
                raise CaptureError("Could not remove the saved automatic paste grant.") from failure

    def clear_focus(self):
        self.paste_target = None
        self.take_field = None
        self.take_window = None
        self.paste_revision += 1

    def watch_focus(self):
        self.clear_focus()
        if self.field:
            self.take_field = self.field.focus
        if self.sway:
            self.take_window = self.sway.focus

    async def output_context(self, read_site, *, annotation=False):
        from linux_call_pages import focused_browser_address, focused_browser_host, process_application

        with suppress(*FOCUS_ERRORS):
            window = await output_window()
            if window is not None:
                app_rules = any(profile.target.lower().startswith("app:") for profile in self.capture_output[2])
                executable = process_application(window.pid) if window.pid and (app_rules or not window.application) else ""
                application = window.application or executable
                if annotation:
                    from portable_terminal_origin import focused_terminal_context

                    terminal = await focused_terminal_context(window, output_window, application)
                    if terminal:
                        return OutputContext(application, terminal=terminal)
                host = await focused_browser_host(window, output_window) if read_site and not annotation else ""
                address = await focused_browser_address(window, output_window) if read_site and annotation else ""
                return OutputContext(application, host, executable, address)
        return OutputContext()

    def prepare_delivery(self, *, raw=False):
        keyboard, shortcuts, revision, terminal = self.paste_target, self.portal, self.paste_revision, self.terminal
        field, focus = self.field, self.take_field
        sway, window = self.sway, self.take_window

        def superseded():
            return self.paste_revision != revision

        def allowed():
            same_field = focus is not None and field is not None and field is self.field and field.focus == focus
            same_window = window is not None and sway is self.sway and sway.focus == window
            return ((not superseded() or same_field or same_window) and self.keyboard is keyboard and self.portal is shortcuts
                    and (shortcuts is not None or self.command_mode))

        async def deliver(text):
            return await self.paste_to(text, keyboard, shortcuts, allowed, terminal, focus, superseded, window, raw=raw)

        return deliver

    async def deliver(self, text):
        return await self.prepare_delivery()(text)

    async def paste_to(self, text, keyboard, shortcuts, allowed, terminal, focus, superseded, window=None, *, raw=False):
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
                                         terminal, focus, superseded, window, raw=raw)

    def close_field(self):
        if self.sway is not None:
            self.sway.close()
            self.sway = None
        if self.field is not None:
            self.field.close()
            self.field = None
        self.last_insertion = None

    async def copy_interrupted_text(self, text):
        self.last_insertion = None
        return await super().copy_interrupted_text(text)

    async def paste_in_field(self, text, keyboard, allowed, terminal, focus, superseded=None, window=None, *, raw=False):
        field = self.field
        sway = self.sway
        previous, self.last_insertion = self.last_insertion, None

        def same_destination():
            return (allowed()
                    and (focus is None or (field is not None and field is self.field and field.focus == focus))
                    and (window is None or (sway is not None and sway is self.sway and sway.focus == window)))

        seeded = focus is not None and isinstance(field, WaylandField) and field.seeded_window is not None

        async def confirm_destination():
            if not same_destination():
                return False
            if seeded and not await field.confirm_seeded_focus(focus):
                return False
            if window is not None and not await sway.confirm(window):
                return False
            return same_destination()

        options = {}
        if window is not None or seeded:
            options["confirm"] = confirm_destination
        if (sway is not None and focus is None and window is None) or not same_destination():
            return PASTE_FALLBACK
        before = await field.snapshot() if focus is not None else None
        if not same_destination():
            return PASTE_FALLBACK
        if before is None or before.focus != focus:
            if window is None and superseded is not None and superseded():
                return PASTE_FALLBACK
            return await keyboard.paste(text, terminal, same_destination, **options)
        inserted = join_dictation(previous[1], text) if not raw and previous and previous[0] == before else text
        blocked = False

        def can_join():
            nonlocal blocked
            valid = same_destination()
            blocked = valid and not field.matches(before)
            return valid and not blocked

        result = await keyboard.paste(inserted, terminal, can_join if inserted != text else same_destination, **options)
        if result != PASTE_SENT:
            if blocked and same_destination():
                return await keyboard.paste(text, terminal, same_destination, **options)
            return result
        if raw:
            return result
        try:
            async with asyncio.timeout(.04):
                after = await field.snapshot()
        except TimeoutError:
            return result
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
