"""Identify call services from browser chrome without retaining page content."""

from dataclasses import dataclass, field
from hashlib import blake2s
import re
from urllib.parse import urlsplit

SITE_NAMES = {
    "google-meet": "Google Meet",
    "banshee": "Banshee",
    "google-voice": "Google Voice",
    "messenger": "Messenger",
    "facetime-web": "FaceTime (web)",
    "groupme": "GroupMe",
}

HOSTS = {
    "banshee.app": "banshee",
    "web.whatsapp.com": "whatsapp",
    "app.v2.gather.town": "gather",
    "app.gather.town": "gather",
    "teams.microsoft.com": "teams",
    "teams.cloud.microsoft": "teams",
    "teams.live.com": "teams",
    "voice.google.com": "google-voice",
    "messenger.com": "messenger",
    "facetime.apple.com": "facetime-web",
    "web.groupme.com": "groupme",
}
MEET_CODE = re.compile(r"[a-z]{3}-[a-z]{4}-[a-z]{3}")
MEET_TITLE = re.compile(r"^Meet\s+[-–—]\s+\S", flags=re.IGNORECASE)
TITLE_SUFFIX = re.compile(
    r"\s+[-–—]\s+(Google Chrome|Chromium|Microsoft Edge|Mozilla Firefox|Brave(?: Browser)?|"
    r"Vivaldi|Helium|Zen(?: Browser| Twilight)?|Arc|Ardia)\s*$", flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class BrowserPage:
    title: str = ""
    address: str = ""


@dataclass(frozen=True)
class Call:
    source: str
    provider: str
    room: str = ""
    verified: bool = field(default=True, compare=False)


def page_identity(address):
    if not address or len(address) > 4096 or any(ord(char) < 32 for char in address):
        return None
    try:
        url = urlsplit(address)
        if url.scheme != "https" or url.username is not None or url.port not in (None, 443):
            return None
        host = (url.hostname or "").lower().rstrip(".").removeprefix("www.")
    except ValueError:
        return None
    if host == "meet.google.com":
        parts = url.path.strip("/").lower().split("/")
        if parts[0] in ("", "landing", "new"):
            return None
        path = next((part for part in parts if MEET_CODE.fullmatch(part)), "/".join(parts))
        # Only a session-local comparison key leaves the classifier.
        room = blake2s(path.encode(), digest_size=12).hexdigest()
        return "google-meet", room
    if host == "webex.com" or host.endswith(".webex.com"):
        return "webex", ""
    provider = HOSTS.get(host)
    return (provider, "") if provider else None


def select_call(source, pages, complete):
    pages = tuple(pages)
    matches = {identity for page in pages if (identity := page_identity(page.address))}
    if len(matches) == 1:
        provider, room = next(iter(matches))
        return Call(source, provider, room, verified=complete)
    # An unrelated page can set its own title. Without an address, a
    # recognizable title can name an offer that still needs confirmation.
    if not matches and any(not page.address and MEET_TITLE.match(
        TITLE_SUFFIX.sub("", page.title.replace("\u200b", "").strip())
    ) for page in pages):
        return Call(source, "google-meet", verified=False)
    return None
