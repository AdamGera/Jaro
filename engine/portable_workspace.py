"""Limit the portable desktop workspace to the session bootstrapped by its owner."""
import hmac
import secrets

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from sdk_embedded import SDK_PATH

BOOTSTRAP = "/_desktop/connect"

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
    def __init__(self, app, *, origin, port, key):
        self.app = app
        self.origin = origin
        self.host = f"127.0.0.1:{port}"
        self.cookie = f"jaro-workspace-{port}"
        self.key = key

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
        elif (path == SDK_PATH + "/identify" and request.method == "GET") or self.valid(request.cookies.get(self.cookie, "")):
            await self.app(scope, receive, send)
            return
        else:
            response = Response(status_code=403)
        await response(scope, receive, send)

    def valid(self, value):
        return hmac.compare_digest(value.encode(), self.key.encode())

    def bootstrap(self, request):
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        if request.method == "GET":
            nonce = secrets.token_urlsafe(18)
            headers["Content-Security-Policy"] = (
                f"default-src 'none'; script-src 'nonce-{nonce}'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
            )
            return HTMLResponse(OPEN_WORKSPACE.format(nonce=nonce), headers=headers)
        if (request.method != "POST" or request.headers.get("origin") != self.origin
                or not self.valid(request.headers.get("x-jaro-workspace-key", ""))):
            return Response(status_code=403, headers=headers)
        response = Response(status_code=204, headers=headers)
        response.set_cookie(self.cookie, self.key, httponly=True, samesite="strict")
        return response


def configure_workspace(app, record, origin):
    key = secrets.token_hex(32)
    record["workspaceKey"] = key
    app.state.portable_workspace = True
    app.state.sdk = record
    app.add_middleware(WorkspaceAccess, origin=origin, port=record["port"], key=key)
