"""Keyboard chord syntax shared by the portable desktop shortcut settings."""
import re

from portable_capture import CaptureError

MODIFIERS = ("Ctrl", "Alt", "Shift", "Super")
KEY_NAMES = {name.casefold(): (name, name) for name in (
    "Left", "Right", "Up", "Down", "Home", "End", "Insert", "Delete", "Tab", "Escape", "Print"
)}
KEY_NAMES.update({
    "pageup": ("PageUp", "Page_Up"), "pagedown": ("PageDown", "Page_Down"),
    "enter": ("Enter", "Return"), "space": ("Space", "space"),
    "backspace": ("Backspace", "BackSpace"),
})


def shortcut_parts(value):
    *modifiers, key = [part.strip().casefold() for part in value.split("+")]
    selected = [name for name in MODIFIERS if name.casefold() in modifiers]
    if len(selected) != len(modifiers):
        raise CaptureError("Use Ctrl, Alt, Shift, or Super once in a shortcut.")
    function = re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", key)
    if function:
        label = keysym = key.upper()
    elif not selected:
        raise CaptureError("Use a modifier with this key, or choose a function key.")
    elif re.fullmatch(r"[a-z0-9]", key):
        label, keysym = key.upper(), key
    elif key in KEY_NAMES:
        label, keysym = KEY_NAMES[key]
    else:
        raise CaptureError("Choose a letter, digit, function key, or named key such as PageUp.")
    return "+".join((*selected, label)), selected, keysym
