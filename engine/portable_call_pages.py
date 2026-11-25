"""Identify call services from browser chrome without retaining page content."""

from dataclasses import dataclass, field
from hashlib import blake2s
import re
from urllib.parse import unquote, urlsplit

SITE_NAMES = {
    "google-meet": "Google Meet",
    "zoom-web": "Zoom (web)",
    "slack-web": "Slack huddle (web)",
    "discord-web": "Discord (web)",
    "jitsi": "Jitsi Meet",
    "banshee": "Banshee",
    "google-voice": "Google Voice",
    "messenger": "Messenger",
    "facetime-web": "FaceTime (web)",
    "groupme": "GroupMe",
}

CONFIRMATION_ONLY_SITES = frozenset(("discord-web",))
ZOOM_DOMAINS = ("zoom.us", "zoom.com", "zoomgov.com")
ZOOM_MEETING = re.compile(r"/wc/([0-9]{9,11})/(?:join|start)/?")
SLACK_HUDDLE = re.compile(r"/huddle/(T[A-Z0-9]+)/([CDG][A-Z0-9]+)/?", re.IGNORECASE)
DISCORD_CHANNEL = re.compile(r"/channels/(?:@me|[0-9]+)/[0-9]+/?")
JITSI_ROOM = re.compile(r"/([\w-][\w -]{0,127})/?")

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
MEET_DISPLAY_ADDRESS = re.compile(r"meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})(?:[?#].*)?")
MEET_TITLE = re.compile(r"^Meet\s+[-–—]\s+\S", flags=re.IGNORECASE)
TITLE_SUFFIX = re.compile(
    r"\s+[-–—]\s+(Google Chrome|Chromium|Microsoft Edge|Mozilla Firefox|Brave(?: Browser)?|"
    r"Vivaldi|Helium|Zen(?: Browser| Twilight)?|Arc|Ardia)\s*$", flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class BrowserPage:
    title: str = ""
    address: str = ""
    room_hint: str = ""


@dataclass(frozen=True)
class Call:
    source: str
    provider: str
    room: str = ""
    verified: bool = field(default=True, compare=False)
    room_hint: str = field(default="", compare=False)


def room_key(path):
    # Only a session-local comparison key leaves the classifier.
    return blake2s(path.encode(), digest_size=12).hexdigest()


def displayed_meet_room(address):
    # Chromium can omit the scheme in display text. That text can identify
    # a room change during a recording, but cannot verify an address to start one.
    match = MEET_DISPLAY_ADDRESS.fullmatch(address)
    return room_key(match[1]) if match else ""


def host_matches(host, domain):
    return host == domain or host.endswith("." + domain)


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
        code = url.path.strip("/").lower().removeprefix("meet/")
        return ("google-meet", room_key(code)) if MEET_CODE.fullmatch(code) else None
    if any(host_matches(host, domain) for domain in ZOOM_DOMAINS):
        match = ZOOM_MEETING.fullmatch(url.path)
        return ("zoom-web", room_key(match[1])) if match else None
    if host == "app.slack.com":
        match = SLACK_HUDDLE.fullmatch(url.path)
        return ("slack-web", room_key("/".join(match.groups()).upper())) if match else None
    if host in ("discord.com", "canary.discord.com", "ptb.discord.com"):
        # Text and voice channels use the same URLs. The address can name
        # the offer, while the user confirms whether they are in a call.
        return ("discord-web", "") if DISCORD_CHANNEL.fullmatch(url.path) else None
    if host == "meet.jit.si":
        match = JITSI_ROOM.fullmatch(unquote(url.path))
        return ("jitsi", room_key(match[1].lower())) if match else None
    if host_matches(host, "webex.com"):
        return "webex", ""
    provider = HOSTS.get(host)
    return (provider, "") if provider else None


def select_call(source, pages, complete):
    pages = tuple(pages)
    matches = {identity for page in pages if (identity := page_identity(page.address))}
    hints = {page.room_hint for page in pages if page.room_hint}
    if len(matches) == 1:
        provider, room = next(iter(matches))
        if hints and (provider != "google-meet" or hints != {room}):
            return None
        hint = room if complete and hints else ""
        verified = complete and not hints and provider not in CONFIRMATION_ONLY_SITES
        return Call(source, provider, room, verified=verified, room_hint=hint)
    if complete and not matches and len(hints) == 1:
        return Call(source, "google-meet", verified=False, room_hint=next(iter(hints)))
    # An unrelated page can set its own title. Without an address, a
    # recognizable title can name an offer that still needs confirmation.
    if not matches and any(not page.address and MEET_TITLE.match(
        TITLE_SUFFIX.sub("", page.title.replace("\u200b", "").strip())
    ) for page in pages):
        return Call(source, "google-meet", verified=False)
    return None
