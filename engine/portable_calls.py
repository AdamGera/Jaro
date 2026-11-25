"""Offer or record recognized microphone activity on the portable desktop."""

import asyncio
from dataclasses import dataclass, replace
from hashlib import sha256
import json
import sqlite3
from time import monotonic, time
from uuid import UUID, uuid4

from fastapi import HTTPException
from portable_capture import CaptureError, SAMPLE_RATE
from portable_call_pages import Call, CONFIRMATION_ONLY_SITES, SITE_NAMES


@dataclass(frozen=True)
class App:
    key: str
    name: str
    binaries: tuple[str, ...]
    delay: float = 2
    browser: bool = False


APPS = (
    App("zoom", "Zoom", ("zoom", "zoom.real", "cpthost")),
    App("teams", "Microsoft Teams", ("ms-teams", "msteams", "teams", "teams-for-linux")),
    App("slack", "Slack", ("slack",)),
    App("discord", "Discord", ("discord", "discordcanary", "discordptb", "discord-canary", "discord-ptb", "com.discordapp.discord"), 20),
    App("webex", "Webex", ("webex", "ciscocollabhost")),
    App("whatsapp", "WhatsApp", ("whatsapp", "whatsapp.root"), 20),
    App("signal", "Signal", ("signal", "signal-desktop"), 20),
    App("telegram", "Telegram", ("telegram", "telegram-desktop"), 20),
    App("gather", "Gather", ("gather", "gather-desktop"), 20),
    App("element", "Element", ("element", "element-desktop"), 20),
    App("line", "LINE", ("line",), 20),
    App("wechat", "WeChat", ("wechat", "weixin"), 20),
    App("chrome", "Chrome", ("chrome", "google-chrome", "google-chrome-stable"), 20, True),
    App("chromium", "Chromium", ("chromium", "chromium-browser"), 20, True),
    App("edge", "Microsoft Edge", ("msedge", "microsoft-edge", "microsoft-edge-stable"), 20, True),
    App("firefox", "Firefox", ("firefox", "firefox-bin", "firefox-esr"), 20, True),
    App("brave", "Brave", ("brave", "brave-browser"), 20, True),
    App("vivaldi", "Vivaldi", ("vivaldi", "vivaldi-bin", "vivaldi-stable", "vivaldi-snapshot"), 20, True),
    App("helium", "Helium", ("helium", "helium-browser"), 20, True),
    App("zen", "Zen", ("zen", "zen-bin"), 20, True),
    App("arc", "Arc", ("arc",), 20, True),
    App("ardia", "Ardia", ("ardia",), 20, True),
)
WEB_PROVIDERS = tuple(App(site, name, (), 20 if site in ("discord-web", "google-voice", "messenger", "groupme") else 2)
                      for site, name in SITE_NAMES.items())
PROVIDERS = {app.key: app for app in (*APPS, *WEB_PROVIDERS)}
BY_BINARY = {binary: app.key for app in APPS for binary in app.binaries}
BROWSER_CONTEXT_BINARIES = frozenset(
    binary for app in APPS if app.browser for binary in app.binaries
) | {"librewolf", "opera", "opera_gx", "thorium"}
DEFAULT = {"enabled": False, "autoStop": False, "stopRequestedCalls": False, "defaultPolicy": "ask", "policies": {}}
POLICIES = ("ask", "never", "auto")
RECENT_SOURCE_LIMIT = 64
RECENT_SOURCE_REFRESH = 3600
POLL_INTERVAL = 1
OBSERVATION_GAP = 5
CALL_DROPOUT = 3
REOFFER_AFTER = 60
OFFER_SECONDS = 20
STOP_GRACE = 30
PAGE_MEMORY = 60
ROOM_CHANGE_DELAY = 4
OFFER_ACTIONS = frozenset(("record", "dismiss", "more"))
POLICY_ACTIONS = frozenset(("back", "always", "never"))


def recognized_providers(binaries):
    names = (name.casefold().removesuffix(".exe") for name in binaries or ())
    return {BY_BINARY[name] for name in names if name in BY_BINARY}


def recording_label(call):
    return PROVIDERS[call.provider].name if call.provider else "Meeting"


async def sample_apps(probe):
    try:
        return await probe()
    except CaptureError:
        return None


@dataclass
class Presence:
    first: float
    last: float
    snooze: float = 0


@dataclass(frozen=True)
class Offer:
    token: str
    call: Call
    expires: float
    choosing: bool = False

    def visible(self, now):
        return self.choosing or now < self.expires


@dataclass
class DetectedRecording:
    id: UUID
    call: Call
    automatic: bool
    started: float
    languages: tuple[str, ...]
    watched_source: str = ""
    microphone_seen: bool = False
    missing: float | None = None
    stop_at: float | None = None
    keep: bool = False
    finishing: bool = False
    changed_room: str = ""
    changed_since: float = 0
    kept_room: str = ""
    ending_reason: str = ""
    title: str = ""

    @property
    def name(self):
        return self.title or recording_label(self.call)


class CallWatcher:
    def __init__(self, state, microphone_users, playback_users, start, finish, available, clock=monotonic,
                 *, browser_pages=None):
        self.state = state
        self.microphone_users = microphone_users
        self.playback_users = playback_users
        self.browser_pages = browser_pages
        self.start = start
        self.finish = finish
        self.available = available
        self.clock = clock
        self.settings_read_error = False
        self.preferences_backup = None
        self.saved_document = {}
        self.preferences, self.recent_sources = self.load_preferences()
        self.seen = {}
        self.pages = {}
        self.offer = None
        self.active = None
        self.message = ""
        self.completed = None
        self.last_observation = None
        self.transition = asyncio.Lock()

    def load_preferences(self):
        try:
            with self.state.library.connection() as db:
                row = db.execute("SELECT preferences FROM portable_call_preferences WHERE id=1").fetchone()
        except (OSError, sqlite3.Error):
            self.settings_read_error = True
            return DEFAULT.copy(), {}
        if not row:
            return DEFAULT.copy(), {}
        raw = row[0]
        try:
            saved = json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            saved = None
        if not isinstance(saved, dict):
            self.preferences_backup = raw
            return DEFAULT.copy(), {}

        self.saved_document = saved
        enabled = saved.get("enabled", False)
        auto_stop = saved.get("autoStop", False)
        requested_stop = saved.get("stopRequestedCalls", False)
        default = saved.get("defaultPolicy", "ask")
        policies = saved.get("policies", {})
        recent = saved.get("recentSources", {})
        preferences = DEFAULT.copy()
        if type(enabled) is bool:
            preferences["enabled"] = enabled
        if type(auto_stop) is bool:
            preferences["autoStop"] = auto_stop
        if type(requested_stop) is bool:
            preferences["stopRequestedCalls"] = requested_stop
        if default in POLICIES and (isinstance(policies, dict) or default != "auto"):
            preferences["defaultPolicy"] = default

        invalid = (
            type(enabled) is not bool or type(auto_stop) is not bool or type(requested_stop) is not bool
            or default not in POLICIES
            or not isinstance(policies, dict) or not isinstance(recent, dict)
        )
        if isinstance(policies, dict):
            rules = {}
            for key, value in policies.items():
                if (app := PROVIDERS.get(key)) is None:
                    continue
                valid = value in POLICIES and not (app.browser and value == "auto")
                rules[key] = value if valid else "ask"
                invalid |= not valid
            preferences["policies"] = rules

        recent_sources = {}
        if isinstance(recent, dict):
            for key, stamp in recent.items():
                if key in PROVIDERS:
                    if type(stamp) is int and 0 < stamp <= 253402300799:
                        recent_sources[key] = stamp
                    else:
                        invalid = True
        if invalid:
            self.preferences_backup = raw
        return preferences, recent_sources

    def settings_warning(self):
        if self.settings_read_error:
            return "Could not read saved call settings. Reopen Jaro before changing them."
        if self.preferences_backup is not None:
            return "Some saved call settings were invalid. Jaro will preserve the original settings when you save a change."
        return ""

    def settings(self, preferences):
        for key, policy in preferences["policies"].items():
            app = PROVIDERS.get(key)
            if app is None or (app.browser and policy == "auto"):
                raise HTTPException(400, "Choose a supported call app and recording policy.")
        self.save_preferences({**self.preferences, **preferences})
        if self.offer and (not preferences["enabled"] or self.policy_for(self.offer.call) == "never"):
            self.offer = None
        if not preferences["enabled"]:
            self.seen = {key: item for key, item in self.seen.items() if item.snooze}
            self.pages.clear()
        self.message = ""
        if self.active and not self.stop_enabled(self.active):
            self.active.stop_at = self.active.missing = None
            self.active.ending_reason = self.active.changed_room = ""

    def save_preferences(self, preferences):
        if self.settings_read_error:
            raise HTTPException(409, self.settings_warning())
        previous_rules = self.saved_document.get("policies")
        unknown = {}
        if isinstance(previous_rules, dict):
            unknown = {key: value for key, value in previous_rules.items() if key not in PROVIDERS}
        document = {**self.saved_document, **preferences, "recentSources": self.recent_sources,
                    "policies": {**unknown, **preferences["policies"]}}
        text = json.dumps(document)
        original = self.preferences_backup
        try:
            with self.state.library.connection() as db:
                if original is None:
                    db.execute("INSERT OR REPLACE INTO portable_call_preferences VALUES(1,?)", (text,))
                else:
                    updated = db.execute("UPDATE portable_call_preferences SET preferences=? WHERE id=1 AND preferences=?",
                                         (text, original))
                    if not updated.rowcount:
                        raise HTTPException(409, "Saved call settings changed. Reopen Jaro before changing them.")
                    data = original if isinstance(original, bytes) else original.encode("utf-8")
                    db.execute("INSERT OR IGNORE INTO portable_call_preferences_recovery VALUES(?,?)",
                               (sha256(data).hexdigest(), original))
        except (OSError, sqlite3.Error):
            raise HTTPException(503, "Could not save the call recording policy.") from None
        self.saved_document = document
        self.preferences_backup = None
        self.preferences = preferences

    def policy_for(self, call):
        policies = self.preferences["policies"]
        return policies.get(call.provider, policies.get(call.source, self.preferences["defaultPolicy"]))

    def remember_sources(self, calls):
        observed_at = int(time())
        updates = {
            call.provider: observed_at for call in calls
            if observed_at - self.recent_sources.get(call.provider, 0) >= RECENT_SOURCE_REFRESH
        }
        if not updates:
            return
        sources = {**self.recent_sources, **updates}
        self.recent_sources = dict(sorted(sources.items(), key=lambda item: (-item[1], item[0]))[:RECENT_SOURCE_LIMIT])
        if self.preferences_backup is None:
            try:
                self.save_preferences(self.preferences)
            except HTTPException:
                # The current list remains useful if the library cannot write it.
                pass

    def visible_offer(self):
        offer = self.offer
        if offer and offer.visible(self.clock()) and self.available():
            return offer
        return None

    def status(self):
        recording = self.active
        active = None
        if recording:
            active = {
                "id": str(recording.id), "name": recording.name, "automatic": recording.automatic,
                "elapsed": max(0, int(self.clock() - recording.started)), "saving": recording.finishing,
                "stopSeconds": max(1, round(recording.stop_at - self.clock())) if recording.stop_at is not None else None,
                "roomChanged": recording.ending_reason == "page",
                "sampleRate": SAMPLE_RATE, "languages": recording.languages,
            }
        offer = self.visible_offer()
        sources = [app for app in (*APPS, *WEB_PROVIDERS)
                   if app.binaries or self.browser_pages or app.key in self.recent_sources or app.key in self.preferences["policies"]]
        sources.sort(key=lambda app: (-self.recent_sources.get(app.key, 0), app.key not in self.preferences["policies"]))
        return {
            **self.preferences, "offer": offer and {
                "token": offer.token, "name": PROVIDERS[offer.call.provider].name,
                "choosing": offer.choosing, "canAlways": offer.call.verified,
            }, "active": active, "completed": self.completed, "message": self.message,
            "settingsWarning": self.settings_warning(),
            "providers": [{"id": app.key, "name": app.name, "browser": app.browser, "website": not app.binaries,
                           "canAlwaysRecord": not app.browser and app.key not in CONFIRMATION_ONLY_SITES,
                           "lastSeen": self.recent_sources.get(app.key)} for app in sources],
        }

    async def answer(self, token, action, languages):
        async with self.transition:
            offer = self.visible_offer()
            if not offer or offer.token != token:
                raise HTTPException(409, "This call is no longer available to record.")
            if action not in (POLICY_ACTIONS if offer.choosing else OFFER_ACTIONS):
                raise HTTPException(409, "The recording offer has changed.")
            if action == "more":
                self.offer = replace(offer, token=uuid4().hex, choosing=True)
                self.message = ""
                return
            if action == "back":
                now = self.clock()
                expires = offer.expires if offer.expires > now else now + OFFER_SECONDS
                self.offer = replace(offer, token=uuid4().hex, expires=expires, choosing=False)
                self.message = ""
                return
            call = offer.call
            if action == "always" and not call.verified:
                raise HTTPException(400, "This browser call needs confirmation each time.")
            if action in ("always", "never"):
                policies = {**self.preferences["policies"], call.provider: "auto" if action == "always" else "never"}
                try:
                    self.save_preferences({**self.preferences, "policies": policies})
                except HTTPException:
                    self.offer = replace(offer, token=uuid4().hex)
                    raise
            self.offer = None
            self.seen[call].snooze = REOFFER_AFTER
            self.message = ""
            if action in ("record", "always"):
                try:
                    await self.begin(call, languages)
                except OSError:
                    raise HTTPException(503, "Could not start recording the detected call.") from None

    def stop_enabled(self, active):
        if active.automatic:
            return self.preferences["enabled"] and self.preferences["autoStop"]
        return bool(active.watched_source) and self.preferences["stopRequestedCalls"]

    async def begin(self, call, languages, *, automatic=False, show_unmatched=True, title=""):
        self.message = ""
        watched = call.source
        if not watched and self.preferences["stopRequestedCalls"]:
            microphones = await sample_apps(self.microphone_users)
            native = [app for app in recognized_providers(microphones) if not PROVIDERS[app].browser]
            if len(native) == 1:
                watched = native[0]
        key = await self.start(languages, call, title=title) if title else await self.start(languages, call)
        if watched or show_unmatched:
            self.active = DetectedRecording(key, call, automatic, self.clock(), languages, watched_source=watched, title=title)
        else:
            self.active = None
        self.offer = None
        return key

    async def stop(self, key, *, monitoring=False):
        async with self.transition:
            await self._finish_active(key, monitoring=monitoring)

    async def stop_requested(self):
        async with self.transition:
            capture = self.state.portable_recording
            if not capture or not capture.meeting:
                return None
            key = capture.key
            if self.active and self.active.id == key:
                await self._finish_active(key)
            else:
                await self.finish(key)
                self.completed = str(key)
            return key

    async def _finish_active(self, key, *, monitoring=False):
        current = self.active
        if not current or current.id != key:
            raise HTTPException(409, "This call is no longer recording.")
        capture = self.state.portable_recording
        call_ended = monitoring and current.stop_at is not None and self.clock() >= current.stop_at
        if monitoring and capture and not capture.worker.done() and not call_ended:
            return
        current.finishing = True
        try:
            await self.finish(key)
        except (HTTPException, OSError):
            self.message = "Could not save the meeting. Open Home to recover the recorded audio."
            raise
        else:
            self.completed = str(key)
        finally:
            if call_ended:
                self.seen.pop(current.call, None)
            elif current.call in self.seen:
                self.seen[current.call].snooze = REOFFER_AFTER
            self.active = None

    def keep_recording(self, key):
        if self.active and self.active.id == key:
            if self.active.ending_reason == "page":
                self.active.kept_room = self.active.changed_room
            else:
                self.active.keep = True
            self.active.stop_at = None
            self.active.ending_reason = ""

    async def run(self):
        while True:
            if self.preferences["enabled"] or self.active:
                detect = self.preferences["enabled"]
                recording = self.active
                watch = recording and self.stop_enabled(recording)
                microphones = await sample_apps(self.microphone_users) if detect or watch else None
                present = recognized_providers(microphones)
                browsers = {key: PROVIDERS[key].binaries for key in sorted(present) if PROVIDERS[key].browser
                            and (recording is None or (recording.automatic and self.preferences["autoStop"]
                                 and recording.call.room and key == recording.call.source))}
                pages = {}
                if browsers and self.browser_pages:
                    try:
                        pages = await self.browser_pages(browsers, microphones)
                    except CaptureError:
                        pass
                playback = None
                if (microphones is not None and watch and recording.watched_source not in present):
                    playback = await sample_apps(self.playback_users)
                async with self.transition:
                    stop = await self.observe(microphones, playback, pages) if self.active is recording else None
                if stop:
                    try:
                        await self.stop(stop, monitoring=True)
                    except (HTTPException, OSError):
                        pass  # The draft remains recoverable.
            await asyncio.sleep(POLL_INTERVAL)

    def identify(self, present, pages, now):
        self.pages = {key: item for key, item in self.pages.items() if key in present and now - item[1] <= PAGE_MEMORY}
        calls = []
        for app in APPS:
            if app.key not in present:
                continue
            fresh = pages.get(app.key)
            remembered = self.pages.get(app.key)
            if fresh and (fresh.verified or not remembered or fresh.provider != remembered[0].provider):
                self.pages[app.key] = (fresh, now)
                calls.append(fresh)
            elif remembered:
                calls.append(replace(remembered[0], verified=False))
            else:
                calls.append(Call(app.key, app.key, verified=not app.browser))
        return calls

    def update_end(self, active, present, playback, page, now):
        on_microphone = active.watched_source in present
        active.microphone_seen |= on_microphone
        audible = on_microphone or active.watched_source in recognized_providers(playback)
        if audible:
            active.keep = False
            if observation := self.seen.get(active.call):
                observation.last = now
        monitoring = self.stop_enabled(active) and (active.automatic or active.microphone_seen)
        if not monitoring or playback is None or audible or active.keep:
            active.missing = None
        elif active.missing is None:
            active.missing = now
        different = ""
        if monitoring and active.automatic and active.call.room and page and page.provider == active.call.provider:
            room = page.room if page.verified else page.room_hint
            if room and room != active.call.room:
                different = room
            if different != active.kept_room:
                active.kept_room = ""
        if different != active.changed_room:
            active.changed_room, active.changed_since = different, now
        reason = ""
        if different and different != active.kept_room and now - active.changed_since >= ROOM_CHANGE_DELAY:
            reason = "page"
        elif active.missing is not None and now - active.missing >= CALL_DROPOUT:
            reason = "audio"
        if not reason:
            active.stop_at = None
        elif active.stop_at is None or reason != active.ending_reason:
            active.stop_at = now + STOP_GRACE
        active.ending_reason = reason
        return active.id if active.stop_at is not None and now >= active.stop_at else None

    async def observe(self, binaries, playback, pages=None):
        now = self.clock()
        detecting = self.preferences["enabled"]
        if not detecting and not (self.active and self.stop_enabled(self.active)):
            binaries = None
        present = recognized_providers(binaries)
        elapsed = now - self.last_observation if self.last_observation is not None else 0
        self.last_observation = now
        if elapsed > OBSERVATION_GAP:
            self.offer = None
            self.pages.clear()
            if self.active:
                self.active.stop_at = self.active.missing = None
                self.active.changed_room = ""
        pages = pages if binaries is not None and pages else {}
        calls = self.identify(present, pages, now) if detecting else []

        if binaries is None:
            self.offer = None
            for key in tuple(self.seen):
                observation = self.seen[key]
                if not observation.snooze:
                    del self.seen[key]
                else:
                    observation.last = now
        else:
            for key in tuple(self.seen):
                observation = self.seen[key]
                if now - observation.last > (observation.snooze or CALL_DROPOUT):
                    del self.seen[key]
            for key in calls:
                observation = self.seen.setdefault(key, Presence(now, now))
                if now - observation.last > OBSERVATION_GAP:
                    observation.first = now
                observation.last = now

        active = self.active
        if active:
            capture = self.state.portable_recording
            if not capture or capture.key != active.id:
                self.active = None
                return None
            if capture.worker.done():
                return active.id
            return self.update_end(active, present, playback if binaries is not None else None, pages.get(active.call.source), now)

        eligible = [call for call in calls if now - self.seen[call].first >= PROVIDERS[call.provider].delay]
        if eligible:
            self.remember_sources(eligible)
        if self.offer:
            offered = self.offer.call
            replaced = any(call.source == offered.source and call != offered for call in calls)
            if not self.offer.visible(now) or offered not in self.seen or replaced or not self.available():
                self.offer = None
            else:
                current = next((call for call in calls if call == offered), None)
                if offered.verified != bool(current and current.verified):
                    # An open offer cannot retain authority from a stale page sample.
                    self.offer = replace(self.offer, call=replace(offered, verified=bool(current and current.verified)))
            return None
        if not self.available():
            return None
        for call in eligible:
            seen = self.seen[call]
            policy = self.policy_for(call)
            if seen.snooze or policy == "never":
                continue
            if policy == "auto" and call.verified:
                seen.snooze = REOFFER_AFTER
                try:
                    await self.begin(call, (), automatic=True)
                except (HTTPException, OSError):
                    self.message = "Could not start recording the detected call."
            else:
                seen.snooze = CALL_DROPOUT
                self.offer = Offer(uuid4().hex, call, now + OFFER_SECONDS)
            break
        return None
