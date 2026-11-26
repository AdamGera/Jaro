"""Limit the desktop workspace to the session bootstrapped by its owner."""
import hmac
import os
from pathlib import Path
import secrets

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from sdk_embedded import SDK_PATH
from engine_lifecycle import WORKSPACE_CONNECT as BOOTSTRAP, workspace_key
from windows_dictation_commands import (ANNOTATION_COMMAND_ROUTE, MEETING_COMMAND_ROUTE, MEETING_TRASH_ROUTE, ONBOARDING_ROUTE,
                                        ROUTE as DICTATION_COMMAND, authorized as authorized_command)

OPEN_WORKSPACE = """
<!doctype html><meta charset="utf-8"><title>Jaro</title>
<p id="status" role="status">Opening Jaro…</p>
<script nonce="{nonce}">
const key = location.hash.slice(1);
history.replaceState(null, "", location.pathname);
fetch(location.pathname, {{ method: "POST", headers: {{ "X-Jaro-Workspace-Key": key }} }})
  .then(response => {{ if (!response.ok) throw new Error(); location.replace("/"); }})
  .catch(() => {{ document.getElementById("status").textContent = "Reopen Jaro to connect to the workspace."; }});
</script>
"""


class WorkspaceAccess:
    def __init__(self, app, *, origin, port, key=None, desktop_key=None, shared_state=None):
        self.app = app
        self.origin = origin
        self.host = f"127.0.0.1:{port}"
        self.cookie = f"jaro-workspace-{port}"
        self.key = key
        self.desktop_key = desktop_key
        self.shared_state = shared_state

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = scope["path"]
        if (request.headers.get("host") != self.host
                or request.headers.get("origin", self.origin) != self.origin):
            response = Response(status_code=403)
        elif path == BOOTSTRAP:
            response = self.bootstrap(request)
        elif self.uses_own_auth(request):
            await self.app(scope, receive, send)
            return
        else:
            token = request.cookies.get(self.cookie, "")
            desktop = self.desktop_token(token)
            if desktop or self.valid(token) or self.native_client(request):
                request.state.portable_desktop = desktop
                await self.app(scope, receive, send)
                return
            if self.shared_state is not None and request.method == "GET" and path in ("/", "/index.html"):
                response = Response("Open Jaro and choose Open in Browser to connect.", status_code=403,
                                    media_type="text/plain", headers={"Cache-Control": "no-store"})
            else:
                response = Response(status_code=403)
        await response(scope, receive, send)

    def valid(self, value):
        if not value:
            return False
        key = self.key if self.shared_state is None else workspace_key(self.shared_state.native_token)
        return hmac.compare_digest(value.encode(), key.encode())

    def desktop_token(self, value):
        return self.desktop_key is not None and hmac.compare_digest(value.encode(), self.desktop_key.encode())

    def native_client(self, request):
        return self.shared_state is not None and hmac.compare_digest(
            request.headers.get("x-jard-token", "").encode(), self.shared_state.native_token.encode(),
        )

    def uses_own_auth(self, request):
        path = request.scope["path"]
        if path.startswith(SDK_PATH + "/"):
            return True
        if self.shared_state is None:
            if path in (DICTATION_COMMAND, MEETING_TRASH_ROUTE, MEETING_COMMAND_ROUTE, ANNOTATION_COMMAND_ROUTE, ONBOARDING_ROUTE):
                return (path == DICTATION_COMMAND and request.method == "GET") or (request.method == "POST" and authorized_command(
                    getattr(request.app.state, "windows_dictation_control", None), request.headers.get("authorization", ""),
                ))
            return False
        return request.method == "GET" and path in (
            "/api/status", "/api/meetings/events",
        )

    def bootstrap(self, request):
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        if request.method == "GET":
            nonce = secrets.token_urlsafe(18)
            headers["Content-Security-Policy"] = (
                f"default-src 'none'; script-src 'nonce-{nonce}'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
            )
            return HTMLResponse(OPEN_WORKSPACE.format(nonce=nonce), headers=headers)
        token = request.headers.get("x-jaro-workspace-key", "")
        if (request.method != "POST" or request.headers.get("origin") != self.origin
                or not (self.desktop_token(token) or self.valid(token))):
            return Response(status_code=403, headers=headers)
        response = Response(status_code=204, headers=headers)
        response.set_cookie(self.cookie, token, httponly=True, samesite="strict")
        return response


def configure_workspace(app, record, origin):
    from linux_direct import STARTUP_ENV, WORKSPACE_CONNECTED

    # The host receives its own credential through the parent-owned ready pipe.
    # A regular workspace cookie never grants ownership of the desktop window.
    key, desktop_key = secrets.token_hex(32), secrets.token_hex(32)
    record["workspaceKey"] = key
    record["workspaceDesktopKey"] = desktop_key
    app.state.portable_workspace = True
    app.state.sdk = record
    if os.environ.get(STARTUP_ENV) and (tray := os.environ.get("JARO_DESKTOP_TRAY")):
        app.state.workspace_startup_receipt = Path(tray) / WORKSPACE_CONNECTED
    app.add_middleware(WorkspaceAccess, origin=origin, port=record["port"], key=key, desktop_key=desktop_key)


def configure_shared_workspace(app, origin, port):
    app.add_middleware(WorkspaceAccess, origin=origin, port=port, shared_state=app.state)
