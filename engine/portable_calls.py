"""Offer or record recognized microphone activity on the portable desktop."""

import asyncio
from dataclasses import dataclass, replace
import json
import sqlite3
from time import monotonic
from uuid import UUID, uuid4

from fastapi import HTTPException
from portable_capture import CaptureError, SAMPLE_RATE
from portable_call_pages import Call, SITE_NAMES


@dataclass(frozen=True)
class App:
    key: str
    name: str
    binaries: tuple[str, ...]
    delay: float = 2
    browser: bool = False


APPS = (
    App("zoom", "Zoom", ("zoom", "zoom.real")),
    App("teams", "Microsoft Teams", ("ms-teams", "msteams", "teams", "teams-for-linux")),
    App("slack", "Slack", ("slack",)),
    App("discord", "Discord", ("discord", "discordcanary", "discordptb"), 20),
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
    App("firefox", "Firefox", ("firefox", "firefox-bin"), 20, True),
    App("brave", "Brave", ("brave", "brave-browser"), 20, True),
    App("vivaldi", "Vivaldi", ("vivaldi", "vivaldi-bin", "vivaldi-stable", "vivaldi-snapshot"), 20, True),
    App("helium", "Helium", ("helium", "helium-browser"), 20, True),
    App("zen", "Zen", ("zen", "zen-bin"), 20, True),
    App("arc", "Arc", ("arc",), 20, True),
    App("ardia", "Ardia", ("ardia",), 20, True),
)
WEB_PROVIDERS = tuple(App(site, name, (), 20 if site in ("google-voice", "messenger", "groupme") else 2)
                      for site, name in SITE_NAMES.items())
PROVIDERS = {app.key: app for app in (*APPS, *WEB_PROVIDERS)}
BY_BINARY = {binary: app.key for app in APPS for binary in app.binaries}
DEFAULT = {"enabled": False, "autoStop": False, "policies": {}}
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
    missing: float | None = None
    stop_at: float | None = None
    keep: bool = False
    finishing: bool = False
    changed_room: str = ""
    changed_since: float = 0
    kept_room: str = ""
    ending_reason: str = ""


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
        with state.library.connection() as db:
            row = db.execute("SELECT preferences FROM portable_call_preferences WHERE id=1").fetchone()
        self.preferences = json.loads(row[0]) if row else DEFAULT
        self.seen = {}
        self.pages = {}
        self.offer = None
        self.active = None
        self.message = ""
        self.completed = None
        self.last_observation = None
        self.transition = asyncio.Lock()

    def settings(self, preferences):
        for key, policy in preferences["policies"].items():
            app = PROVIDERS.get(key)
            if app is None or (app.browser and policy == "auto"):
                raise HTTPException(400, "Choose a supported call app and recording policy.")
        self.save_preferences(preferences)
        self.offer = None
        if not preferences["enabled"]:
            self.seen = {key: item for key, item in self.seen.items() if item.snooze}
            self.pages.clear()
        self.message = ""
        if not (preferences["enabled"] and preferences["autoStop"]) and self.active:
            self.active.stop_at = self.active.missing = None
            self.active.ending_reason = self.active.changed_room = ""

    def save_preferences(self, preferences):
        try:
            with self.state.library.connection() as db:
                db.execute("INSERT OR REPLACE INTO portable_call_preferences VALUES(1,?)", (json.dumps(preferences),))
        except (OSError, sqlite3.Error):
            raise HTTPException(503, "Could not save the call recording policy.") from None
        self.preferences = preferences

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
                "id": str(recording.id), "name": recording_label(recording.call), "automatic": recording.automatic,
                "elapsed": max(0, int(self.clock() - recording.started)), "saving": recording.finishing,
                "stopSeconds": max(1, round(recording.stop_at - self.clock())) if recording.stop_at is not None else None,
                "roomChanged": recording.ending_reason == "page",
                "sampleRate": SAMPLE_RATE, "languages": recording.languages,
            }
        offer = self.visible_offer()
        return {
            **self.preferences, "offer": offer and {
                "token": offer.token, "name": PROVIDERS[offer.call.provider].name,
                "choosing": offer.choosing, "canAlways": offer.call.verified,
            }, "active": active, "completed": self.completed, "message": self.message,
            "providers": [{"id": app.key, "name": app.name, "browser": app.browser}
                          for app in (*APPS, *WEB_PROVIDERS) if app.binaries or self.browser_pages],
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

    async def begin(self, call, languages, *, automatic=False):
        self.message = ""
        key = await self.start(languages, call)
        self.active = DetectedRecording(key, call, automatic, self.clock(), languages)

    async def stop(self, key, *, monitoring=False):
        async with self.transition:
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
                microphones = await sample_apps(self.microphone_users) if self.preferences["enabled"] else None
                present = recognized_providers(microphones)
                recording = self.active
                browsers = {key: PROVIDERS[key].binaries for key in sorted(present) if PROVIDERS[key].browser
                            and (recording is None or (recording.automatic and self.preferences["autoStop"]
                                 and recording.call.room and key == recording.call.source))}
                pages = {}
                if browsers and self.browser_pages:
                    try:
                        pages = await self.browser_pages(browsers)
                    except CaptureError:
                        pass
                active = self.active
                playback = None
                if (microphones is not None and active and active.automatic and self.preferences["autoStop"]
                        and active.call.source not in present):
                    playback = await sample_apps(self.playback_users)
                async with self.transition:
                    stop = await self.observe(microphones, playback if self.active is active else None, pages)
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
        playing = active.call.source in recognized_providers(playback)
        audible = active.call.source in present or playing
        if audible:
            active.keep = False
            if observation := self.seen.get(active.call):
                observation.last = now
        monitoring = active.automatic and self.preferences["enabled"] and self.preferences["autoStop"]
        if not monitoring or playback is None or audible or active.keep:
            active.missing = None
        elif active.missing is None:
            active.missing = now
        different = ""
        if (monitoring and active.call.room and page and page.verified and page.room
                and page.provider == active.call.provider):
            if page.room != active.call.room:
                different = page.room
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
        if not self.preferences["enabled"]:
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
        calls = self.identify(present, pages, now)

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

        if self.offer:
            offered = self.offer.call
            replaced = any(call.source == offered.source and call != offered for call in calls)
            if not self.offer.visible(now) or offered not in self.seen or replaced or not self.available():
                self.offer = None
            return None
        if not self.available():
            return None
        policies = self.preferences["policies"]
        for call in calls:
            seen = self.seen[call]
            policy = policies.get(call.provider, policies.get(call.source, "ask"))
            if seen.snooze or policy == "never" or now - seen.first < PROVIDERS[call.provider].delay:
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
