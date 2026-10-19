"""Persistent conversation and mutation checkpoints in the agent's own SQLite DB.

A sent-but-unconfirmed operation is never automatically replayed. Recovery needs an
authoritative, operation-specific receipt from the corresponding adapter.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time

from .models import AmbiguousOperation, BudgetExceeded, NotSent


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Memory:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_absolute() or self.path.name != "agent.sqlite3":
            raise ValueError("AGENT_DATABASE_PATH_INVALID")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for part in (self.path.parent, *self.path.parent.parents):
            s = part.lstat()
            if not stat.S_ISDIR(s.st_mode):
                raise ValueError("AGENT_DATABASE_PARENT_UNSAFE")
        s = self.path.parent.lstat()
        if s.st_uid != os.geteuid() or stat.S_IMODE(s.st_mode) != 0o700:
            raise ValueError("AGENT_DATABASE_DIRECTORY_UNSAFE")
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            s = os.fstat(fd)
            if (not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or
                    s.st_uid != os.geteuid() or stat.S_IMODE(s.st_mode) != 0o600):
                raise ValueError("AGENT_DATABASE_FILE_UNSAFE")
        finally:
            os.close(fd)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), timeout=3, check_same_thread=False,
                                  isolation_level=None)
        self.db.execute("PRAGMA trusted_schema=OFF")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations(
                operation_id TEXT PRIMARY KEY, kind TEXT NOT NULL, request_hash TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('STARTED','DONE','UNKNOWN')),
                result TEXT, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(
                seq INTEGER PRIMARY KEY, conversation_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                text TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS budgets(
                day TEXT PRIMARY KEY, requests INTEGER NOT NULL,
                input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL);
        """)

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def put(self, key, value):
        encoded = canonical(value)
        if len(encoded.encode()) > 262144:
            raise ValueError("MEMORY_VALUE_TOO_LARGE")
        with self.lock:
            self.db.execute("INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, encoded))

    def operation(self, operation_id, kind, payload, execute):
        digest = hashlib.sha256(canonical(payload).encode()).hexdigest()
        with self.transaction():
            row = self.db.execute("SELECT kind,request_hash,state,result FROM operations WHERE operation_id=?",
                                  (operation_id,)).fetchone()
            if row:
                if row[0] != kind or row[1] != digest:
                    raise ValueError("OPERATION_ID_REUSE_MISMATCH")
                if row[2] == "DONE":
                    value = json.loads(row[3])
                    if isinstance(value, dict) and set(value) == {"_agent_not_sent"}:
                        raise NotSent(value["_agent_not_sent"])
                    return value
                raise AmbiguousOperation("OPERATION_REQUIRES_RECEIPT_RECONCILIATION")
            self.db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?)",
                            (operation_id, kind, digest, "STARTED", None, time.time()))
        try:
            result = execute()
            encoded = canonical(result)
            if len(encoded.encode()) > 262144:
                raise ValueError("OPERATION_RESULT_TOO_LARGE")
        except NotSent as error:
            # Preserve a conclusive negative receipt. The controller may abandon
            # this cycle, but it never resets or replays its operation ID.
            with self.lock:
                self.db.execute("UPDATE operations SET state='DONE',result=?,updated_at=? WHERE operation_id=?",
                    (canonical({"_agent_not_sent": str(error)}), time.time(), operation_id))
            raise
        except BaseException:
            with self.lock:
                self.db.execute("UPDATE operations SET state='UNKNOWN',updated_at=? WHERE operation_id=?",
                                (time.time(), operation_id))
            raise
        with self.lock:
            self.db.execute("UPDATE operations SET state='DONE',result=?,updated_at=? WHERE operation_id=?",
                            (encoded, time.time(), operation_id))
        return result

    def reconcile(self, operation_id, kind, request_payload, result, *, receipt_verified=False):
        """Adapters may restore an exact result only after verifying their own receipt.

        This is deliberately not a retry/reset API. Unknown deliveries remain blocked
        when the provider cannot prove which message was sent.
        """
        if receipt_verified is not True:
            raise AmbiguousOperation("AUTHORITATIVE_RECEIPT_REQUIRED")
        digest = hashlib.sha256(canonical(request_payload).encode()).hexdigest()
        encoded = canonical(result)
        with self.transaction():
            row = self.db.execute("SELECT kind,request_hash,state FROM operations WHERE operation_id=?",
                                  (operation_id,)).fetchone()
            if not row or row[:2] != (kind, digest) or row[2] not in ("STARTED", "UNKNOWN"):
                raise ValueError("RECONCILIATION_BINDING_INVALID")
            self.db.execute("UPDATE operations SET state='DONE',result=?,updated_at=? WHERE operation_id=?",
                            (encoded, time.time(), operation_id))

    def append(self, conversation_id, role, text, now):
        if role not in ("user", "assistant") or not isinstance(text, str) or not 0 < len(text) <= 65536:
            raise ValueError("CONVERSATION_MESSAGE_INVALID")
        with self.lock:
            self.db.execute("INSERT INTO messages(conversation_id,role,text,created_at) VALUES(?,?,?,?)",
                            (conversation_id, role, text, now))

    def history(self, conversation_id, max_chars=16000, max_messages=12):
        with self.lock:
            rows = self.db.execute("SELECT role,text FROM messages WHERE conversation_id=? ORDER BY seq DESC LIMIT ?",
                                   (conversation_id, max_messages)).fetchall()
        selected, total = [], 0
        for role, text in rows:
            if len(text) + total > max_chars:
                break
            selected.append({"role": role, "content": text})
            total += len(text)
        return list(reversed(selected))

    def reserve_budget(self, day, input_tokens, output_tokens, *, max_requests, max_input_tokens, max_output_tokens):
        if min(input_tokens, output_tokens) < 1:
            raise ValueError("BUDGET_RESERVATION_INVALID")
        with self.transaction():
            row = self.db.execute("SELECT requests,input_tokens,output_tokens FROM budgets WHERE day=?", (day,)).fetchone()
            row = row or (0, 0, 0)
            updated = (row[0] + 1, row[1] + input_tokens, row[2] + output_tokens)
            if updated[0] > max_requests or updated[1] > max_input_tokens or updated[2] > max_output_tokens:
                raise BudgetExceeded("DAILY_MODEL_BUDGET_EXHAUSTED")
            self.db.execute("INSERT INTO budgets VALUES(?,?,?,?) ON CONFLICT(day) DO UPDATE SET requests=excluded.requests,input_tokens=excluded.input_tokens,output_tokens=excluded.output_tokens",
                            (day, *updated))
        return updated

    def counts(self):
        with self.lock:
            operations = dict(self.db.execute("SELECT state,COUNT(*) FROM operations GROUP BY state"))
            messages = self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        return {"operations": operations, "messages": messages}

    def close(self):
        with self.lock:
            self.db.close()
