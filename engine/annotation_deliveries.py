"""Private receipts for annotation prompts sent to OpenCode."""

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
from time import monotonic, time
from uuid import uuid4

from annotation_assets import terminal_path, terminal_scope
from annotation_projects import PROJECT_TARGET
from annotation_sites import site_candidates

UNDO_SECONDS = 10 * 60
KEEP_SECONDS = 7 * 24 * 60 * 60
ROUTE_LIFETIME = 24 * 60 * 60
ROLLBACK_REVIEW = ("rollback-unconfirmed", "rollback-staged", "rollback-commit-unconfirmed")


class ReceiptError(ValueError):
    pass


class AnnotationDeliveries:
    def __init__(self, root):
        self.path = Path(root) / ".annotation-deliveries.sqlite3"

    @contextmanager
    def connection(self, *, write=False):
        try:
            if sys.platform == "win32":
                from windows_handoff_identity import open_private_file

                descriptor = open_private_file(self.path, write=write)
            else:
                flags = os.O_RDWR | os.O_CREAT if write else os.O_RDONLY
                descriptor = os.open(self.path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        except FileNotFoundError:
            if write:
                raise ReceiptError("The annotation receipt store is unavailable.") from None
            yield None
            return
        except (OSError, ValueError) as error:
            raise ReceiptError("The annotation receipt store is unavailable.") from error
        try:
            if sys.platform != "win32":
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
                    raise ReceiptError("The annotation receipt store is unavailable.")
        finally:
            os.close(descriptor)
        mode = "rw" if write else "ro"
        try:
            with closing(sqlite3.connect(self.path.absolute().as_uri() + "?mode=" + mode, uri=True, timeout=1.5)) as db:
                db.row_factory = sqlite3.Row
                with db:
                    if write:
                        db.execute("PRAGMA secure_delete=ON")
                        db.execute("""CREATE TABLE IF NOT EXISTS deliveries(
                            id TEXT PRIMARY KEY, annotation TEXT NOT NULL, service TEXT NOT NULL,
                            destination TEXT NOT NULL, message TEXT, state TEXT NOT NULL,
                            created REAL NOT NULL, included_sheet INTEGER NOT NULL, correction_message TEXT, remembered_rule TEXT
                        )""")
                        db.execute("CREATE INDEX IF NOT EXISTS deliveries_by_annotation ON deliveries(annotation,created)")
                        db.execute("""CREATE TABLE IF NOT EXISTS annotation_rules(
                            kind TEXT NOT NULL, source TEXT NOT NULL, id TEXT NOT NULL UNIQUE,
                            title TEXT NOT NULL, target TEXT NOT NULL, channel TEXT NOT NULL,
                            automatic INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL DEFAULT 0,
                            PRIMARY KEY(kind,source)
                        )""")
                        db.execute("""CREATE TABLE IF NOT EXISTS annotation_outbox(
                            capture TEXT PRIMARY KEY, origin TEXT NOT NULL, rule TEXT NOT NULL,
                            state TEXT NOT NULL, message TEXT NOT NULL, created REAL NOT NULL,
                            delivery TEXT
                        )""")
                        self.migrate_columns(db)
                    yield db
        except sqlite3.Error as error:
            raise ReceiptError("The annotation receipt store is unavailable.") from error

    @staticmethod
    def migrate_columns(db):
        migrations = {
            "deliveries": {"correction_message": "TEXT", "remembered_rule": "TEXT"},
            "annotation_rules": {"automatic": "INTEGER NOT NULL DEFAULT 0", "updated": "REAL NOT NULL DEFAULT 0"},
        }
        locked = False
        for table, columns in migrations.items():
            present = {column["name"] for column in db.execute(f"PRAGMA table_info({table})")}
            if columns.keys() <= present:
                continue
            if not locked:
                db.execute("BEGIN IMMEDIATE")
                locked = True
                present = {column["name"] for column in db.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in present:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    @staticmethod
    def insert_delivery(db, annotation, service, destination, included_sheet):
        identifier = str(uuid4())
        now = time()
        db.execute("DELETE FROM deliveries WHERE created < ?", (now - KEEP_SECONDS,))
        db.execute("""INSERT INTO deliveries(id,annotation,service,destination,state,created,included_sheet)
                      VALUES(?,?,?,?,'pending',?,?)""",
                   (identifier, annotation, service, json.dumps(destination, ensure_ascii=False), now, int(included_sheet)))
        return identifier

    def begin(self, annotation, service, destination, included_sheet, *, remember_scope=None):
        with self.connection(write=True) as db:
            identifier = self.insert_delivery(db, annotation, service, destination, included_sheet)
            if remember_scope:
                previous = self.exact_rule(db, remember_scope)
                change = {"scope": remember_scope, "previous": dict(previous) if previous else None, "status": "pending"}
                db.execute("UPDATE deliveries SET remembered_rule=? WHERE id=?", (json.dumps(change), identifier))
            return identifier

    @staticmethod
    def exact_rule(db, scope):
        return db.execute("SELECT * FROM annotation_rules WHERE kind=? AND source=?", (scope["kind"], scope["key"])).fetchone()

    @staticmethod
    def write_rule(db, *, kind, source, id, title, target, channel, automatic, updated):
        db.execute("""INSERT INTO annotation_rules(kind,source,id,title,target,channel,automatic,updated)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(kind,source) DO UPDATE SET id=excluded.id,title=excluded.title,
                target=excluded.target,channel=excluded.channel,automatic=excluded.automatic,updated=excluded.updated""",
                   (kind, source, id, title, target, channel, int(automatic), updated))

    def confirm_remembered_delivery(self, identifier, message, channel):
        with self.connection(write=True) as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM deliveries WHERE id=?", (identifier,)).fetchone()
            if not row or not row["remembered_rule"] or row["state"] != "pending":
                raise ReceiptError("The pending destination change is unavailable.")
            change = json.loads(row["remembered_rule"])
            scope, previous = change["scope"], change["previous"]
            current = self.exact_rule(db, scope)
            if (current["id"] if current else None) == (previous["id"] if previous else None):
                destination = json.loads(row["destination"])
                self.write_rule(db, kind=scope["kind"], source=scope["key"], id=identifier, title=destination["title"],
                                target=destination["id"], channel=channel, automatic=True, updated=time())
                change["status"] = "saved"
            else:
                change["status"] = "changed"
            return self.encode(db.execute("""UPDATE deliveries SET state='queued',message=?,remembered_rule=?
                WHERE id=? RETURNING *""", (message, json.dumps(change), identifier)).fetchone())

    def undo_remembered_rule(self, identifier):
        with self.connection(write=True) as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM deliveries WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise ReceiptError("Annotation delivery receipt not found.")
            change = json.loads(row["remembered_rule"]) if row["remembered_rule"] else None
            if not change or change["status"] != "saved" or time() >= row["created"] + UNDO_SECONDS:
                return self.encode(row)
            scope, previous = change["scope"], change["previous"]
            current = self.exact_rule(db, scope)
            if not current or current["id"] != identifier:
                change["status"] = "changed"
            elif previous:
                # Reactivate the previous destination without reviving its old pending approvals.
                self.write_rule(db, **{**previous, "id": str(uuid4()), "updated": time()})
                change["status"] = "restored"
            else:
                db.execute("DELETE FROM annotation_rules WHERE id=?", (identifier,))
                change["status"] = "removed"
            return self.encode(db.execute("UPDATE deliveries SET remembered_rule=? WHERE id=? RETURNING *",
                                          (json.dumps(change), identifier)).fetchone())

    def change(self, identifier, state, message=None, *, correction_message=None):
        with self.connection(write=True) as db:
            row = db.execute("""UPDATE deliveries SET state=?, message=coalesce(?,message),
                correction_message=coalesce(?,correction_message) WHERE id=? RETURNING *""",
                             (state, message, correction_message, identifier)).fetchone()
            if row is None:
                raise ReceiptError("Annotation delivery receipt not found.")
            return self.encode(row)

    def get(self, identifier):
        with self.connection() as db:
            if db is None or not self.has_table(db):
                return None
            row = db.execute("SELECT * FROM deliveries WHERE id=? AND created>=?",
                             (identifier, time() - KEEP_SECONDS)).fetchone()
            return self.encode(row) if row else None

    def recent(self, annotation):
        with self.connection() as db:
            if db is None or not self.has_table(db):
                return []
            rows = db.execute("""SELECT * FROM deliveries WHERE annotation=? AND created>=?
                ORDER BY created DESC,id DESC LIMIT 20""", (annotation, time() - KEEP_SECONDS))
            return [self.encode(row) for row in rows]

    def preferred_rule(self, scopes):
        with self.connection() as db:
            if db is None or not self.has_table(db, "annotation_rules"):
                return None
            return self.match_rule(db, scopes)

    def list_rules(self):
        with self.connection() as db:
            if db is None or not self.has_table(db, "annotation_rules"):
                return []
            rows = db.execute("SELECT * FROM annotation_rules ORDER BY kind, source")
            return [{"id": row["id"], "kind": row["kind"], "source": row["source"], "title": row["title"],
                     "target": row["target"], "channel": row["channel"],
                     "automatic": "automatic" in row.keys() and bool(row["automatic"])} for row in rows]

    def manage_rule(self, identifier, *, automatic=None):
        with self.connection(write=True) as db:
            if automatic is None:
                changed = db.execute("DELETE FROM annotation_rules WHERE id=?", (identifier,))
            elif type(automatic) is bool:
                # A pending delivery or stale settings window holds the previous ID.
                changed = db.execute("UPDATE annotation_rules SET id=?, automatic=?, updated=? WHERE id=?",
                                     (str(uuid4()), int(automatic), time(), identifier))
            else:
                raise ReceiptError("Choose whether to send annotations automatically.")
            if not changed.rowcount:
                raise ReceiptError("This rule has changed. Reopen the rules list to see the current destination.")

    def retarget_rule(self, identifier, title, target, channel):
        with self.connection(write=True) as db:
            # Rotate the ID so stale settings and pending deliveries can't use the new target.
            changed = db.execute("""UPDATE annotation_rules SET id=?, title=?, target=?, channel=?, automatic=0, updated=?
                WHERE id=?""", (str(uuid4()), title, target, channel, time(), identifier))
            if not changed.rowcount:
                raise ReceiptError("This rule has changed. Reopen the rules list to see the current destination.")

    @staticmethod
    def scope_candidates(scopes):
        for scope in scopes:
            if scope["kind"] == "site" and "location" in scope:
                for key in site_candidates(scope["location"]):
                    yield {"kind": "site", "key": key, "label": key}
            elif scope["kind"] == "terminal" and (folder := terminal_path(scope.get("location", scope["key"]))):
                for candidate in (folder, *folder.parents):
                    if not terminal_path(str(candidate)):
                        break
                    yield terminal_scope(candidate)
            else:
                yield scope

    @classmethod
    def match_rule(cls, db, scopes):
        for scope in cls.scope_candidates(scopes):
            row = cls.exact_rule(db, scope)
            if row:
                return {"scope": scope, "id": row["id"], "title": row["title"], "target": row["target"],
                        "channel": row["channel"], "automatic": bool(row["automatic"]) if "automatic" in row.keys() else False,
                        "updated": row["updated"] if "updated" in row.keys() else 0}
        return None

    def remember_rule(self, scope, title, target, channel, automatic=False, *, create_only=False):
        identifier = str(uuid4())
        updated = time()
        with self.connection(write=True) as db:
            if create_only:
                if not db.in_transaction:
                    db.execute("BEGIN IMMEDIATE")
                if self.exact_rule(db, scope):
                    raise ReceiptError("This source already has a rule. Change its chat from the rules list.")
            self.write_rule(db, kind=scope["kind"], source=scope["key"], id=identifier, title=title, target=target,
                            channel=channel, automatic=automatic, updated=updated)
        return {"id": identifier, "scope": scope, "title": title, "target": target, "channel": channel,
                "automatic": automatic, "updated": updated}

    def forget_rule(self, scopes, identifier):
        with self.connection(write=True) as db:
            for scope in self.scope_candidates(scopes):
                if db.execute("DELETE FROM annotation_rules WHERE id=? AND kind=? AND source=?",
                              (identifier, scope["kind"], scope["key"])).rowcount:
                    return True
        return False

    def enqueue_route(self, capture, origin, rule, limit):
        with self.connection(write=True) as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            now = time()
            db.execute("DELETE FROM annotation_outbox WHERE created < ?", (now - KEEP_SECONDS,))
            live = db.execute("SELECT count(*) FROM annotation_outbox WHERE state IN ('pending','sending')").fetchone()[0]
            if live >= limit:
                return False
            return bool(db.execute("""INSERT OR IGNORE INTO annotation_outbox VALUES(?,?,?,'pending',?,?,NULL)""",
                                   (capture, json.dumps(origin), json.dumps(rule), "Waiting for narration.", now)).rowcount)

    @staticmethod
    def decode_route(row):
        return {**dict(row), "origin": json.loads(row["origin"]), "rule": json.loads(row["rule"])}

    def get_route(self, capture):
        with self.connection() as db:
            if db is None or not self.has_table(db, "annotation_outbox"):
                return None
            row = db.execute("SELECT * FROM annotation_outbox WHERE capture=? AND created>=?",
                             (capture, time() - KEEP_SECONDS)).fetchone()
            return self.decode_route(row) if row else None

    def set_pending_route(self, capture, state, message):
        if not self.get_route(capture):
            return False
        with self.connection(write=True) as db:
            return bool(db.execute("""UPDATE annotation_outbox SET state=?,message=?
                                   WHERE capture=? AND state='pending'""", (state, message, capture)).rowcount)

    def claim_route(self, capture, scopes, rule_id, service, destination, included_sheet, *,
                    confirmed_before=None, confirmed_project=None):
        with self.connection(write=True) as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,created FROM annotation_outbox WHERE capture=?", (capture,)).fetchone()
            if not row or row["state"] != "pending":
                raise ReceiptError("The automatic send was cancelled or already attempted.")
            if row["created"] + ROUTE_LIFETIME <= time():
                raise ReceiptError("The pending automatic send expired.")
            rule = self.match_rule(db, scopes)
            target = confirmed_project if confirmed_project and confirmed_project.startswith(PROJECT_TARGET) else destination["id"]
            if not rule or not rule["automatic"] or rule["id"] != rule_id or rule["target"] != target:
                raise ReceiptError("The saved destination changed before sending.")
            if confirmed_before is not None and monotonic() >= confirmed_before:
                raise ReceiptError("The automatic send panel’s confirmation expired.")
            delivery = self.insert_delivery(db, capture, service, destination, included_sheet)
            db.execute("""UPDATE annotation_outbox SET state='sending',message='Sending annotation…',delivery=?
                          WHERE capture=?""", (delivery, capture))
            return delivery

    @staticmethod
    def reconcile_claims(db, capture=None):
        condition = " AND capture=?" if capture is not None else ""
        parameters = (capture,) if capture is not None else ()
        for row in db.execute("""SELECT q.capture,d.state FROM annotation_outbox q LEFT JOIN deliveries d
                              ON d.id=q.delivery WHERE q.state='sending'""" + condition, parameters).fetchall():
            state = row["state"]
            if state in ("queued", "withdrawn", "received", "missing", "corrected", "withdrawal-unconfirmed",
                         "correction-unconfirmed", "reverted", "rollback-files", *ROLLBACK_REVIEW):
                route, message = "sent", "Annotation accepted by OpenCode."
            elif state == "refused":
                route, message = "failed", "OpenCode did not accept the annotation."
            else:
                route, message = "unconfirmed", "Delivery unconfirmed. Check OpenCode for the annotation."
            db.execute("UPDATE annotation_outbox SET state=?,message=? WHERE capture=?",
                       (route, message, row["capture"]))

    def settle_route(self, capture):
        with self.connection(write=True) as db:
            self.reconcile_claims(db, capture)
            row = db.execute("SELECT * FROM annotation_outbox WHERE capture=?", (capture,)).fetchone()
            return self.decode_route(row) if row else None

    def recover_routes(self):
        with self.connection() as db:
            if db is None or not self.has_table(db, "annotation_outbox"):
                return []
        with self.connection(write=True) as db:
            self.reconcile_claims(db)
            db.execute("""UPDATE annotation_outbox SET state='failed',message='The pending automatic send expired.'
                          WHERE state='pending' AND created<=?""", (time() - ROUTE_LIFETIME,))
            rows = db.execute("SELECT * FROM annotation_outbox WHERE state='pending' ORDER BY created")
            return [self.decode_route(row) for row in rows]

    @staticmethod
    def has_table(db, name="deliveries"):
        return db.execute("SELECT 1 FROM sqlite_schema WHERE name=? AND type='table'", (name,)).fetchone() is not None

    @staticmethod
    def encode(row):
        due = row["created"] + UNDO_SECONDS
        state = "unconfirmed" if row["state"] == "pending" else row["state"]
        change = json.loads(row["remembered_rule"]) if "remembered_rule" in row.keys() and row["remembered_rule"] else None
        can_withdraw = state in ("queued", "withdrawal-unconfirmed", "rollback-files") and row["message"]
        return {"deliveryId": row["id"], "annotation": row["annotation"], "service": row["service"],
                "session": json.loads(row["destination"]), "message": row["message"], "status": state,
                "includedSheet": bool(row["included_sheet"]), "createdAt": int(row["created"] * 1000),
                "undoUntil": int(due * 1000),
                "correctionMessage": row["correction_message"] if "correction_message" in row.keys() else None,
                "canCorrect": bool(state in ("received", "queued", "withdrawal-unconfirmed", "rollback-files") and row["message"]),
                "canUndo": bool((can_withdraw or (change and change["status"] == "saved")) and time() < due),
                **({"rememberedRule": {"status": change["status"], "label": change["scope"]["label"]}} if change else {})}
