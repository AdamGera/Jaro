"""Discover local OpenCode v2 sessions and deliver a user-selected annotation."""

import asyncio
from dataclasses import dataclass, field
from hashlib import sha256
from ipaddress import ip_address
from itertools import islice
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
from time import time
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from annotation_assets import TERMINAL_APPS, terminal_path
from annotation_deliveries import AnnotationDeliveries, ReceiptError
from managed_preferences import require_opencode
from saved_annotations import annotation_context, annotation_detail, inline_contact_sheet
from session_store import MissingSession, SessionStore

SESSION_ID = re.compile(r"ses[A-Za-z0-9_-]{1,127}")
SERVICE_ID = re.compile(r"[0-9a-f]{64}")
MESSAGE_ID = re.compile(r"msg[A-Za-z0-9_-]{1,127}")
MAX_REGISTRATION = 64 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
PAGE_SIZE = 100
PAGE_COUNT = 5


class RoutingError(ValueError):
    pass


class RouteUnavailable(RoutingError):
    """OpenCode could not be reached before the prompt was attempted."""


@dataclass(frozen=True)
class Registration:
    origin: str
    address: str
    port: int
    pid: int
    version: str | None
    password: str | None = field(repr=False)
    channel: str

    @property
    def key(self):
        return sha256(f"{self.origin}|{self.pid}|{self.version}".encode()).hexdigest()

    @property
    def auth(self):
        return ("opencode", self.password) if self.password else None


def state_directory():
    root = os.environ.get("XDG_STATE_HOME")
    return (Path(root) if root else Path.home() / ".local" / "state") / "opencode"


def read_registration(path):
    # Do not follow links or open a named pipe in place of a registration.
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            details = os.fstat(source.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid() or details.st_mode & 0o077:
                return None
            raw = source.read(MAX_REGISTRATION + 1)
        if len(raw) > MAX_REGISTRATION:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict):
            return None
        url = urlsplit(value["url"])
        if url.scheme != "http" or url.username is not None or url.password is not None or url.path not in ("", "/"):
            return None
        if url.query or url.fragment or url.hostname is None:
            return None
        ip = ip_address("127.0.0.1" if url.hostname == "localhost" else url.hostname)
        if ip.is_unspecified:
            ip = ip_address("127.0.0.1" if ip.version == 4 else "::1")
        pid = value["pid"]
        version = value.get("version")
        password = value.get("password")
        port = 80 if url.port is None else url.port
        if (not ip.is_loopback or "%" in str(ip) or type(pid) is not int or not 0 < pid <= 2**31 - 1 or not port or
                (version is not None and (not isinstance(version, str) or len(version) > 160)) or
                (password is not None and not isinstance(password, str))):
            return None
        host = f"[{ip}]" if ip.version == 6 else str(ip)
        return Registration(f"http://{host}:{port}", host, port, pid, version,
                            password or os.environ.get("OPENCODE_PASSWORD"), path.name)
    except (OSError, TypeError, ValueError, KeyError, UnicodeError):
        return None


def registrations(directory):
    try:
        with os.scandir(directory) as entries:
            alternatives = sorted(entry.name for entry in islice(entries, 512)
                                  if entry.name.startswith("service-") and entry.name.endswith(".json"))
    except OSError:
        return []
    return [record for name in ["service.json", *alternatives[:15]]
            if (record := read_registration(directory / name)) is not None]


def owns_listener(record):
    try:
        rows = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-a", "-p", str(record.pid), f"-iTCP:{record.port}", "-sTCP:LISTEN", "-Fpun"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=1.5, check=False,
        ).stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return False
    bound = {f"n{record.address}:{record.port}", f"n*:{record.port}"}
    return f"p{record.pid}" in rows and f"u{os.geteuid()}" in rows and bool(bound.intersection(rows))


async def request(client, record, method, path, *, seconds, **options):
    require_opencode()
    async with asyncio.timeout(seconds):
        async with client.stream(method, record.origin + path, auth=record.auth, **options) as response:
            status = response.status_code
            if not 200 <= status < 300:
                return status, b""
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data += chunk
                if len(data) > MAX_RESPONSE:
                    raise RoutingError("OpenCode returned a response that was too large.")
            return status, data


def decoded(body):
    try:
        return json.loads(body)
    except (ValueError, UnicodeError) as error:
        raise RoutingError("OpenCode returned an unreadable response.") from error


def value_of(data):
    return data.get("data", data) if isinstance(data, dict) else data


async def discover(client, expected, directory, *, channel=None):
    require_opencode()
    problem = "OpenCode v2 isn’t running locally."
    failure = RouteUnavailable
    try:
        async with asyncio.timeout(3.5):
            for record in await asyncio.to_thread(registrations, directory):
                if (expected and record.key != expected) or (channel and channel != record.channel):
                    continue
                if not await asyncio.to_thread(owns_listener, record):
                    continue
                try:
                    status, body = await request(client, record, "GET", "/api/info", seconds=1.5)
                    info = value_of(decoded(body)) if status == 200 else None
                except (TimeoutError, httpx.HTTPError, RoutingError):
                    continue
                if status in (401, 403):
                    problem = "OpenCode rejected the credentials in its local registration."
                    failure = RoutingError
                elif status == 404:
                    problem = "The local OpenCode service doesn’t support the v2 session API."
                    failure = RoutingError
                if (isinstance(info, dict) and type(info.get("pid")) is int and info["pid"] == record.pid and
                        isinstance(info.get("version"), str) and (record.version is None or info["version"] == record.version)):
                    return record
    except TimeoutError:
        pass
    if expected:
        problem = "That OpenCode service is no longer available. Refresh the destinations and choose again."
    raise failure(problem)


def display(value, limit):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def session(value):
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not SESSION_ID.fullmatch(value["id"]):
        return None
    time = value.get("time")
    if value.get("parentID") is not None or (isinstance(time, dict) and time.get("archived") is not None):
        return None
    location = value.get("location")
    directory = location.get("directory") if isinstance(location, dict) else None
    directory = directory or value.get("directory")
    if not isinstance(directory, str) or len(directory) > 4096 or any(ord(char) < 32 for char in directory):
        directory = ""
    return {"id": value["id"], "title": display(value.get("title"), 180) or value["id"], "directory": directory}


def check_status(status):
    if status in (401, 403):
        raise RoutingError("OpenCode rejected the credentials in its local registration.")
    if not 200 <= status < 300:
        raise RoutingError("OpenCode could not provide the requested session information.")



async def session_record(client, record, path, expected_id):
    try:
        status, response = await request(client, record, "GET", path, seconds=2)
    except (TimeoutError, httpx.HTTPError) as error:
        raise RouteUnavailable("Could not verify the selected OpenCode session.") from error
    if status == 404:
        raise RoutingError("That OpenCode session is no longer available.")
    if status >= 500:
        raise RouteUnavailable("OpenCode is temporarily unavailable.")
    check_status(status)
    value = value_of(decoded(response))
    destination = session(value)
    if not destination or destination["id"] != expected_id:
        raise RoutingError("That OpenCode session is no longer available.")
    return value


async def current_session(client, record, path, expected_id):
    value = await session_record(client, record, path, expected_id)
    if value.get("revert") is not None:
        raise RoutingError("Resolve the staged revert in OpenCode before sending a prompt to this session.")
    return session(value)


def accepted_message(response):
    try:
        value = value_of(decoded(response)) if response else None
    except RoutingError:
        return None  # A successful status is sufficient even when no receipt was returned.
    message = value.get("id") if isinstance(value, dict) else None
    return message if isinstance(message, str) and MESSAGE_ID.fullmatch(message) else None

async def list_sessions(client, record, query, limit):
    results, seen, cursors = [], set(), set()
    terms = query.casefold().split()
    params = {"parentID": "null", "limit": PAGE_SIZE}
    for _ in range(PAGE_COUNT):
        status, body = await request(client, record, "GET", "/api/session", seconds=2, params=params)
        check_status(status)
        data = decoded(body)
        rows = value_of(data)
        if not isinstance(rows, list):
            raise RoutingError("OpenCode returned an unreadable session list.")
        for value in rows:
            item = session(value)
            if not item or item["id"] in seen:
                continue
            seen.add(item["id"])
            if all(term in " ".join(item.values()).casefold() for term in terms):
                results.append(item)
                if len(results) == limit:
                    return results
        cursor = data.get("cursor") if isinstance(data, dict) else None
        next_cursor = cursor.get("next") if isinstance(cursor, dict) else None
        if not rows or not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 4096 or next_cursor in cursors:
            break
        cursors.add(next_cursor)
        params = {"cursor": next_cursor, "limit": PAGE_SIZE}
    return results


def make_client():
    # OpenCode is resolved to a literal loopback address before reaching this client.
    return httpx.AsyncClient(trust_env=False, follow_redirects=False, headers={"Accept-Encoding": "identity"})


def capture_for_routing(root, identifier):
    if not isinstance(identifier, str) or not 1 <= len(identifier) <= 36:
        raise RoutingError("Enter a saved annotation ID or latest.")
    try:
        return annotation_detail(SessionStore(root, read_only=True), identifier)
    except MissingSession as error:
        raise RoutingError("Annotation not found.") from error
    except sqlite3.Error as error:
        raise RoutingError("The saved annotation library is unavailable.") from error


def route_scopes(detail):
    if not (origin := detail.get("origin")):
        return []
    if origin["app"] in TERMINAL_APPS:
        project = terminal_path(origin.get("terminal", {}).get("project"))
        return [{"kind": "terminal", "key": str(project), "label": project.name}] if project else []
    site = origin.get("site")
    scopes = [{"kind": "site", "key": site, "label": site}] if site else []
    scopes.append({"kind": "app", "key": origin["app"], "label": origin["name"]})
    return scopes


def words(text):
    return " ".join(re.findall(r"[^\W_]+", text.casefold()))


def terminal_suggestion(origin, rows):
    pane = origin.get("terminal", {})
    title, target = pane.get("title"), pane.get("session")
    if title:
        prefix = title.removesuffix("…") if title.endswith("…") else None
        matches = [row for row in rows if row["title"] == title or
                   (prefix and row["title"].startswith(prefix) and len(row["title"]) > len(prefix))]
        for key, value in (("directory", pane["folder"]), ("id", target)):
            if len(matches) > 1 and value:
                narrowed = [row for row in matches if row[key] == value]
                matches = narrowed or matches
        if len(matches) == 1:
            return {"sessionId": matches[0]["id"], "reason": "Open in the captured terminal"}
        if matches:
            return None
    if target and any(row["id"] == target for row in rows):
        return {"sessionId": target, "reason": "OpenCode session in the terminal"}
    return None


def suggested_session(detail, rows):
    origin = detail.get("origin", {})
    if suggestion := terminal_suggestion(origin, rows):
        return suggestion
    text = " " + words(detail["transcript"][:20_000]) + " "
    mentioned = []
    for row in rows:
        title = words(row["title"])
        if row["id"] != row["title"] and len(title) >= 5 and f" {title} " in text:
            mentioned.append((len(title), row["id"]))
    mentioned.sort(reverse=True)
    if mentioned and (len(mentioned) == 1 or mentioned[0][0] > mentioned[1][0]):
        return {"sessionId": mentioned[0][1], "reason": "Mentioned in the annotation"}
    if origin.get("app") in TERMINAL_APPS:
        root = terminal_path(origin.get("terminal", {}).get("project"))
        if root:
            same_project = [row for row in rows if (directory := terminal_path(row["directory"])) and directory.is_relative_to(root)]
            if len(same_project) == 1:
                return {"sessionId": same_project[0]["id"], "reason": "Terminal project matches"}
        return None
    app = words(origin.get("name", ""))
    if app:
        project = [row for row in rows if row["directory"] and words(Path(row["directory"]).name) == app]
        if len(project) == 1:
            return {"sessionId": project[0]["id"], "reason": "Project matches the captured app"}
    return None


async def destinations(query="", *, directory=None, root=None, capture=None):
    require_opencode()
    if not isinstance(query, str) or len(query) > 300:
        raise RoutingError("Enter an OpenCode search of up to 300 characters.")
    detail = await asyncio.to_thread(capture_for_routing, root, capture) if capture is not None else None
    scopes = route_scopes(detail) if detail else []
    rule = await asyncio.to_thread(AnnotationDeliveries(root).preferred_rule, scopes) if scopes else None
    directory = directory or state_directory()
    async with make_client() as client:
        record = None
        if rule:
            try:
                record = await discover(client, None, directory, channel=rule["channel"])
            except RoutingError:
                pass  # The recent picker is still usable when a saved channel is offline.
        try:
            record = record or await discover(client, None, directory)
            async with asyncio.timeout(5):
                rows = await list_sessions(client, record, query, 25)
        except (TimeoutError, httpx.HTTPError, RoutingError) as error:
            warning = str(error) if isinstance(error, RoutingError) else "OpenCode did not finish listing sessions. Try again."
            if not detail:
                raise RoutingError(warning) from error
            return {"service": None, "sessions": [], "scopes": scopes, "suggestion": None, "warning": warning,
                    "rule": {**rule, "available": False} if rule else None}
        if not detail:
            return {"service": record.key, "sessions": rows}
        suggestion = None
        if rule:
            saved = None
            if rule["channel"] == record.channel and SESSION_ID.fullmatch(rule["target"]):
                try:
                    saved = await current_session(client, record, "/api/session/" + rule["target"], rule["target"])
                except RoutingError:
                    pass
            rule = {**rule, "available": bool(saved)}
            if saved and not query.strip():
                rows = [saved, *(row for row in rows if row["id"] != saved["id"])][:25]
            if saved and any(row["id"] == saved["id"] for row in rows):
                suggestion = {"sessionId": saved["id"], "reason": f"Saved for {rule['scope']['label']}"}
        if not suggestion:
            terminal = detail.get("origin", {}).get("terminal", {})
            target = terminal.get("session")
            if not query.strip() and target and not any(row["id"] == target for row in rows):
                try:
                    chosen = await current_session(client, record, "/api/session/" + target, target)
                    rows = [*rows[:24], chosen]
                except RoutingError:
                    pass
            suggestion = suggested_session(detail, rows)
        if suggestion and rows[0]["id"] != suggestion["sessionId"]:
            rows.sort(key=lambda row: row["id"] != suggestion["sessionId"])
        return {"service": record.key, "sessions": rows, "rule": rule, "scopes": scopes, "suggestion": suggestion}


async def set_destination_rule(root, capture, scope_kind, target, service, automatic=False, *, directory=None):
    require_opencode()
    if type(automatic) is not bool:
        raise RoutingError("Choose whether this destination sends automatically.")
    if not isinstance(target, str) or not SESSION_ID.fullmatch(target):
        raise RoutingError("Choose a valid OpenCode session ID.")
    if not isinstance(service, str) or not SERVICE_ID.fullmatch(service):
        raise RoutingError("Refresh the OpenCode destinations and choose again.")
    detail = await asyncio.to_thread(capture_for_routing, root, capture)
    scope = next((item for item in route_scopes(detail) if item["kind"] == scope_kind), None)
    if scope is None:
        raise RoutingError("This annotation does not have that captured app or site.")
    async with make_client() as client:
        record = await discover(client, service, directory or state_directory())
        chosen = await current_session(client, record, f"/api/session/{target}", target)
    return await asyncio.to_thread(AnnotationDeliveries(root).remember_rule, scope, chosen["title"], target, record.channel, automatic)


async def resolve_automatic_rule(rule):
    require_opencode()
    async with make_client() as client:
        record = await discover(client, None, state_directory(), channel=rule["channel"])
        target = rule["target"]
        if not SESSION_ID.fullmatch(target):
            raise RoutingError("The automatic OpenCode destination is invalid.")
        chosen = await current_session(client, record, f"/api/session/{target}", target)
        return record.key, chosen


def forget_destination_rule(root, capture, rule_id):
    try:
        identifier = str(UUID(rule_id))
    except (TypeError, ValueError, AttributeError) as error:
        raise RoutingError("Choose a valid OpenCode destination rule.") from error
    scopes = route_scopes(capture_for_routing(root, capture))
    return {"forgotten": AnnotationDeliveries(root).forget_rule(scopes, identifier)}


def prompt(root, identifier):
    library = SessionStore(root, read_only=True)
    try:
        detail = annotation_detail(library, identifier)
    except sqlite3.Error as error:
        raise RoutingError("The saved annotation library is unavailable.") from error
    markdown = annotation_context(detail)["markdown"]
    if len(json.dumps(markdown, ensure_ascii=False).encode()) > 3 * 1024 * 1024:
        raise RoutingError("This annotation is too long to send. Use Copy for Agent or jardctl annotate context.")
    image = inline_contact_sheet(library, detail["id"])
    files = [{"uri": f"data:image/png;base64,{image}", "name": "contact-sheet.png",
              "description": "Keyframes from the Jard screen annotation in chronological order."}] if image else []
    return {"text": markdown, "files": files, "delivery": "queue"}, detail["id"]


async def send_session_prompt(body, target, service):
    require_opencode()
    if not isinstance(target, str) or not SESSION_ID.fullmatch(target) or not isinstance(service, str) or not SERVICE_ID.fullmatch(service):
        raise RoutingError("Refresh the OpenCode destinations and choose a session.")
    async with make_client() as client:
        record = await discover(client, service, state_directory())
        path = f"/api/session/{target}"
        await current_session(client, record, path, target)
        if not await asyncio.to_thread(owns_listener, record):
            raise RouteUnavailable("OpenCode closed before Jaro sent the session.")
        try:
            status, _ = await request(client, record, "POST", path + "/prompt", seconds=5, json=body)
        except (TimeoutError, httpx.HTTPError) as error:
            raise RoutingError("OpenCode did not confirm delivery. Check the session before trying again.") from error
        if status >= 500:
            raise RoutingError("OpenCode did not confirm delivery. Check the session before trying again.")
        if not 200 <= status < 300:
            raise RoutingError("OpenCode declined the session context.")


async def send_annotation(root, identifier, target, service=None, *, directory=None, authorize=None, remember_scope=None, body=None):
    require_opencode()
    if not isinstance(identifier, str) or len(identifier) > 36:
        raise RoutingError("Enter a saved annotation ID or latest.")
    if not isinstance(target, str) or not SESSION_ID.fullmatch(target):
        raise RoutingError("Choose a valid OpenCode session ID.")
    if service is not None and (not isinstance(service, str) or not SERVICE_ID.fullmatch(service)):
        raise RoutingError("Refresh the OpenCode destinations and choose again.")
    if body is None:
        body, capture = await asyncio.to_thread(prompt, root, identifier)
    else:
        capture = str(UUID(identifier))
    async with make_client() as client:
        record = await discover(client, service, directory or state_directory())
        path = f"/api/session/{target}"
        destination = await current_session(client, record, path, target)
        if not await asyncio.to_thread(owns_listener, record):
            raise RouteUnavailable("OpenCode closed before Jard sent the annotation.")
        receipts = AnnotationDeliveries(root)
        if authorize:
            delivery = await authorize(record.key, destination, bool(body["files"]))
        else:
            delivery = await asyncio.to_thread(receipts.begin, capture, record.key, destination, bool(body["files"]),
                                               remember_scope=remember_scope)
        try:
            status, response = await request(client, record, "POST", path + "/prompt", seconds=5, json=body)
        except httpx.ConnectError as error:
            await asyncio.to_thread(receipts.change, delivery, "refused")
            raise RoutingError("OpenCode closed before Jard sent the annotation.") from error
        except (TimeoutError, httpx.HTTPError, RoutingError) as error:
            raise RoutingError("OpenCode did not confirm delivery. Check the session before trying again.") from error
        if status >= 500:
            raise RoutingError("OpenCode did not confirm delivery. Check the session before trying again.")
        if not 200 <= status < 300:
            await asyncio.to_thread(receipts.change, delivery, "refused")
            raise RoutingError("OpenCode refused the annotation. The saved capture is still available in Jard.")
        try:
            message = accepted_message(response)
            if remember_scope:
                receipt = await asyncio.to_thread(receipts.confirm_remembered_delivery, delivery, message, record.channel)
            else:
                receipt = await asyncio.to_thread(receipts.change, delivery, "queued", message)
        except ReceiptError as error:
            raise RoutingError("OpenCode accepted the annotation. Jard could not save the receipt.") from error
        return {**receipt, "delivery": "queued"}


def recent_deliveries(root, identifier):
    if not isinstance(identifier, str) or len(identifier) > 36:
        raise RoutingError("Enter a saved annotation ID or latest.")
    try:
        key = str(UUID(identifier))
    except ValueError:
        try:
            key = annotation_detail(SessionStore(root, read_only=True), identifier)["id"]
        except sqlite3.Error as error:
            raise RoutingError("The saved annotation library is unavailable.") from error
    return {"deliveries": AnnotationDeliveries(root).recent(key)}


async def delivery_location(client, record, path, message):
    status, body = await request(client, record, "GET", path + "/inbox", seconds=1.5)
    if status == 404:
        return "unavailable"
    check_status(status)
    inbox = value_of(decoded(body))
    if not isinstance(inbox, list):
        raise RoutingError("OpenCode returned an unreadable inbox.")
    if any(isinstance(item, dict) and item.get("id") == message for item in inbox):
        return "waiting"

    params = {"order": "desc", "limit": PAGE_SIZE}
    cursors = set()
    for _ in range(PAGE_COUNT):
        status, body = await request(client, record, "GET", path + "/message", seconds=1.5, params=params)
        if status == 404:
            return "unavailable"
        check_status(status)
        data = decoded(body)
        messages = value_of(data)
        if not isinstance(messages, list):
            raise RoutingError("OpenCode returned an unreadable session timeline.")
        for item in messages:
            if isinstance(item, dict) and item.get("id") == message:
                return "received" if item.get("type") == "user" else "unavailable"
        cursor = data.get("cursor") if isinstance(data, dict) else None
        next_cursor = cursor.get("next") if isinstance(cursor, dict) else None
        if not next_cursor:
            return "absent"
        if not messages or not isinstance(next_cursor, str) or len(next_cursor) > 4096 or next_cursor in cursors:
            break
        cursors.add(next_cursor)
        params = {"cursor": next_cursor, "limit": PAGE_SIZE}
    return "unavailable"


async def read_receipt(root, delivery):
    try:
        identifier = str(UUID(delivery))
    except (TypeError, ValueError, AttributeError) as error:
        raise RoutingError("Enter a valid annotation delivery receipt ID.") from error
    receipts = AnnotationDeliveries(root)
    saved = await asyncio.to_thread(receipts.get, identifier)
    if not saved:
        raise RoutingError("Annotation delivery receipt not found.")
    return receipts, saved


def receipt_target(saved):
    target, message = saved["session"]["id"], saved["message"]
    if not SESSION_ID.fullmatch(target) or not MESSAGE_ID.fullmatch(message) or not SERVICE_ID.fullmatch(saved["service"]):
        raise RoutingError("This annotation’s OpenCode receipt is invalid.")
    return target, message


async def withdraw_annotation(root, delivery, *, directory=None):
    require_opencode()
    receipts, saved = await read_receipt(root, delivery)
    identifier = saved["deliveryId"]
    if saved["status"] in ("withdrawn", "received", "missing", "corrected", "correction-unconfirmed"):
        return saved
    if not saved["canUndo"] or not saved["message"]:
        raise RoutingError("This annotation can no longer be withdrawn from the OpenCode inbox.")
    target, message = receipt_target(saved)

    async with make_client() as client:
        record = await discover(client, saved["service"], directory or state_directory())
        path = f"/api/session/{target}"
        try:
            async with asyncio.timeout(3):
                location = await delivery_location(client, record, path, message)
                if location == "absent":
                    # During delivery the item can briefly appear in neither response.
                    location = await delivery_location(client, record, path, message)
        except (httpx.HTTPError, TimeoutError) as error:
            raise RoutingError("Could not check whether this annotation is still queued. Try again.") from error
        if location == "received":
            return await asyncio.to_thread(receipts.change, identifier, "received")
        if location == "absent" and saved["status"] == "queued":
            return await asyncio.to_thread(receipts.change, identifier, "missing")
        if location == "absent" and saved["status"] == "withdrawal-unconfirmed":
            return saved
        if location != "waiting":
            raise RoutingError("OpenCode could not confirm where the annotation is.")
        if not await asyncio.to_thread(owns_listener, record):
            raise RoutingError("OpenCode closed before Jard could withdraw the annotation.")
        if time() * 1000 >= saved["undoUntil"]:
            raise RoutingError("The ten-minute withdrawal window has ended.")

        unknown = await asyncio.to_thread(receipts.change, identifier, "withdrawal-unconfirmed")
        try:
            status, _ = await request(client, record, "DELETE", path + f"/inbox/{message}", seconds=2.5)
            async with asyncio.timeout(3):
                location = await delivery_location(client, record, path, message)
                if location == "absent":
                    location = await delivery_location(client, record, path, message)
        except (httpx.HTTPError, TimeoutError, RoutingError):
            return unknown
        if location == "received":
            return await asyncio.to_thread(receipts.change, identifier, "received")
        if 200 <= status < 300 and location == "absent":
            return await asyncio.to_thread(receipts.change, identifier, "withdrawn")
        return unknown


async def correct_annotation(root, delivery, *, directory=None):
    require_opencode()
    receipts, saved = await read_receipt(root, delivery)
    if saved["status"] in ("corrected", "correction-unconfirmed"):
        return saved
    if not saved["canCorrect"]:
        raise RoutingError("This receipt does not have a delivered OpenCode message to correct.")
    target, message = receipt_target(saved)
    identifier = saved["deliveryId"]
    async with make_client() as client:
        record = await discover(client, saved["service"], directory or state_directory())
        path = f"/api/session/{target}"
        try:
            async with asyncio.timeout(3):
                location = await delivery_location(client, record, path, message)
        except (httpx.HTTPError, TimeoutError) as error:
            raise RoutingError("Could not verify the original OpenCode message. Try again.") from error
        if location == "waiting":
            raise RoutingError("The annotation is still in the OpenCode inbox. Use Undo send if it is available.")
        if location != "received":
            raise RoutingError("OpenCode could not verify the original annotation in the conversation.")
        if not await asyncio.to_thread(owns_listener, record):
            raise RoutingError("OpenCode closed before Jard could send the correction.")
        await current_session(client, record, path, target)

        body = {"text": f"Please disregard the Jard screen annotation in message `{message}` "
                        f"(capture `{saved['annotation']}`). I sent that annotation by mistake. "
                        "This correction applies only to that annotation.",
                "delivery": "steer"}
        unknown = await asyncio.to_thread(receipts.change, identifier, "correction-unconfirmed")
        try:
            status, response = await request(client, record, "POST", path + "/prompt", seconds=5, json=body)
        except httpx.ConnectError as error:
            await asyncio.to_thread(receipts.change, identifier, "received")
            raise RoutingError("OpenCode closed before Jard sent the correction.") from error
        except (TimeoutError, httpx.HTTPError, RoutingError):
            return unknown
        if status >= 500:
            return unknown
        if not 200 <= status < 300:
            await asyncio.to_thread(receipts.change, identifier, "received")
            raise RoutingError("OpenCode refused the correction.")
        return await asyncio.to_thread(receipts.change, identifier, "corrected", correction_message=accepted_message(response))
