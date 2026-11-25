"""Validation shared by the live and offline dictation settings commands."""
import json

from dictation_output import parse_profiles
from rumble_model import LANGUAGES

SETTINGS_LIMIT = 56_000


def validate_changes(changes):
    if not isinstance(changes, dict) or not changes or len(changes) > 64:
        raise ValueError("Provide a nonempty settings object with up to 64 keys.")
    encoded = json.dumps(changes, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(encoded) > SETTINGS_LIMIT:
        raise ValueError("The settings document exceeds 56,000 bytes.")
    if "dictation.outputProfiles" in changes:
        parse_profiles(changes["dictation.outputProfiles"])
    if "dictation.languages" in changes:
        languages = changes["dictation.languages"]
        if not isinstance(languages, list) or any(not isinstance(code, str) or code not in LANGUAGES for code in languages):
            raise ValueError("Choose supported dictation languages.")
    return encoded
