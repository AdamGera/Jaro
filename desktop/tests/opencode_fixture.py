"""An isolated OpenCode v2 protocol fixture for annotation delivery."""

from base64 import b64encode
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from threading import Thread
from urllib.parse import parse_qs, urlsplit


@contextmanager
def local_opencode(directory):
    project = {"id": "ses_project", "title": "Release review", "location": {"directory": "/fixture/Jard"}}
    later = {"id": "ses_later", "title": "Roadmap", "directory": "/fixture/Planning"}
    auth = "Basic " + b64encode(b"opencode:disposable-fixture").decode()
    requests, posted = [], []
    outcome = {"status": 200}
    inbox, timeline = ["msg_other"], ["msg_prior"]
    outcome["inbox"] = inbox

    class Service(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, value, **headers):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, content in headers.items():
                self.send_header(name, content)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlsplit(self.path)
            requests.append((self.command, url.path))
            if self.headers.get("Authorization") != auth:
                self.respond(401, {})
            elif url.path == "/api/info":
                self.respond(200, {"pid": os.getpid(), "version": "2.0.0-fixture"})
            elif url.path == "/api/session":
                query = parse_qs(url.query)
                if "project_rows" in outcome:
                    self.respond(200, {"data": outcome["project_rows"]})
                elif query.get("cursor") == ["page-two"]:
                    self.respond(200, {"data": [project, later]})
                else:
                    self.respond(200, {"data": [project, {"id": "ses_archived", "time": {"archived": 1}},
                                                 {"id": "ses_child", "parentID": project["id"]},
                                                 {"id": "ses_./unsafe"}], "cursor": {"next": "page-two"}})
            elif "project_rows" in outcome and url.path.rsplit("/", 1)[-1] in outcome.get("project_details", {}):
                self.respond(200, {"data": outcome["project_details"][url.path.rsplit("/", 1)[-1]]})
            elif url.path == "/api/session/ses_project":
                details = {**project, "revert": {"messageID": "msg_prior"}} if outcome.get("staged_revert") else project
                self.respond(200, {"data": details})
            elif url.path == "/api/session/ses_project/inbox":
                if outcome.get("unreadable"):
                    self.respond(503, {})
                else:
                    self.respond(200, {"data": [{"id": item} for item in inbox]})
            elif url.path == "/api/session/ses_project/message":
                self.respond(200, {"data": [{"id": item, "type": "user"} for item in timeline]})
            elif url.path == "/api/session/ses_later":
                self.respond(404, {}) if outcome.get("retired_session") else self.respond(200, {"data": later})
            elif url.path == "/api/session/ses_redirect":
                self.respond(200, {"data": {"id": "ses_redirect", "title": "Redirect"}})
            else:
                self.respond(404, {})

        def do_POST(self):
            requests.append((self.command, self.path))
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Authorization") != auth:
                self.respond(401, {})
                return
            posted.append((self.path, json.loads(raw)))
            if self.path == "/api/session/ses_redirect/prompt":
                self.respond(307, {}, Location="/api/session/ses_project/prompt")
                return
            message = "msg_fixture" if len(posted) == 1 else f"msg_fixture_{len(posted)}"
            if outcome["status"] == 200:
                inbox.append(message)
            self.respond(outcome["status"], {"data": {"id": message}})

        def do_DELETE(self):
            requests.append((self.command, self.path))
            if self.headers.get("Authorization") != auth:
                self.respond(401, {})
                return
            base = "/api/session/ses_project/inbox/"
            message = self.path.removeprefix(base)
            if not self.path.startswith(base) or message not in inbox:
                self.respond(404, {})
                return
            inbox.remove(message)
            if outcome.get("race"):
                timeline.insert(0, message)
            if outcome.get("lose_confirmation"):
                outcome["unreadable"] = True
            self.respond(200, {})

    directory.mkdir(parents=True, exist_ok=True)
    registration = directory / "service.json"
    with ThreadingHTTPServer(("127.0.0.1", 0), Service) as server:
        with registration.open("x") as stream:
            registration.chmod(0o600)
            json.dump({"url": f"http://127.0.0.1:{server.server_port}", "pid": os.getpid(),
                       "version": "2.0.0-fixture", "password": "disposable-fixture"}, stream)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield registration, requests, posted, outcome
        finally:
            server.shutdown()
            thread.join()
            registration.unlink(missing_ok=True)
