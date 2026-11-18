import hmac
import json
import os
import sqlite3
import time
from pathlib import Path

from cryptography.fernet import Fernet


def load_key(bot_id, database_path):
    if os.name == "nt":
        key_dir = Path(os.environ["LOCALAPPDATA"]) / "Dialogspy"
    else:
        config_dir = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
        key_dir = Path(config_dir) / "dialogspy"
    key_path = key_dir / f"{bot_id}.key"
    if not key_path.exists():
        if database_path.exists():
            raise RuntimeError(f"Для существующего архива нужен ключ {key_path}")
        key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as key_file:
                key_file.write(Fernet.generate_key())
    return key_path.read_bytes()


class Archive:
    def __init__(self, path, key):
        self.cipher = Fernet(key)
        self.index_key = hmac.digest(key, b"Dialogspy message index", "sha256")
        self.db = sqlite3.connect(path)
        try:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, data BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    message_key BLOB NOT NULL,
                    chat_key BLOB,
                    kind TEXT NOT NULL,
                    data BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_message ON events(message_key, id);
                CREATE TABLE IF NOT EXISTS processed (
                    update_id INTEGER PRIMARY KEY,
                    received INTEGER NOT NULL DEFAULT (unixepoch())
                );
                CREATE INDEX IF NOT EXISTS processed_received ON processed(received);
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY,
                    data BLOB NOT NULL,
                    ready_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS downloads (
                    id TEXT PRIMARY KEY,
                    data BLOB NOT NULL,
                    retry_at INTEGER NOT NULL DEFAULT 0
                );
            """)
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(events)")}
            if "chat_key" not in columns:
                self.db.execute("ALTER TABLE events ADD COLUMN chat_key BLOB")
            self.db.execute("CREATE INDEX IF NOT EXISTS events_chat ON events(chat_key, id)")
            outbox_columns = {row[1] for row in self.db.execute("PRAGMA table_info(outbox)")}
            if "ready_at" not in outbox_columns:
                self.db.execute("ALTER TABLE outbox ADD COLUMN ready_at REAL NOT NULL DEFAULT 0")
            if self.setting("archive") is None:
                with self.db:
                    self.set_setting("archive", True)
        except BaseException:
            self.db.close()
            raise

    def encrypt(self, value):
        return self.cipher.encrypt(json.dumps(value, ensure_ascii=False).encode())

    def decrypt(self, value):
        return json.loads(self.cipher.decrypt(value))

    def setting(self, name):
        row = self.db.execute("SELECT data FROM settings WHERE name = ?", (name,)).fetchone()
        return self.decrypt(row[0]) if row else None

    def set_setting(self, name, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (name, self.encrypt(value)))

    def accept_update(self, update_id):
        self.db.execute("DELETE FROM processed WHERE received < unixepoch() - 172800")
        result = self.db.execute("INSERT OR IGNORE INTO processed(update_id) VALUES (?)", (update_id,))
        return result.rowcount == 1

    def message_key(self, chat_id, message_id):
        return hmac.digest(self.index_key, f"{chat_id}/{message_id}".encode(), "sha256")

    def chat_key(self, chat_id):
        return hmac.digest(self.index_key, f"chat/{chat_id}".encode(), "sha256")

    def media_key(self, attachment):
        identity = f"media/{attachment['type']}/{attachment['identity']}"
        return hmac.digest(self.index_key, identity.encode(), "sha256").hex()

    def last_message_event(self, chat_id, message_id):
        row = self.db.execute(
            "SELECT data FROM events WHERE message_key = ? ORDER BY id DESC LIMIT 1",
            (self.message_key(chat_id, message_id),),
        ).fetchone()
        return self.decrypt(row[0]) if row else None

    def save_event(self, chat_id, event):
        result = self.db.execute(
            "INSERT INTO events(message_key, chat_key, kind, data) VALUES (?, ?, ?, ?)",
            (self.message_key(chat_id, event["message_id"]), self.chat_key(chat_id),
             event["kind"], self.encrypt(event)),
        )
        return result.lastrowid

    def context(self, chat_id, message_id):
        chat_key = self.chat_key(chat_id)
        message_key = self.message_key(chat_id, message_id)
        origin = self.db.execute("SELECT min(id) FROM events WHERE message_key = ?", (message_key,)).fetchone()[0]
        if origin is None:
            return []
        before = self.db.execute(
            "SELECT data FROM events WHERE chat_key = ? AND kind = 'new' AND id < ? ORDER BY id DESC LIMIT 2",
            (chat_key, origin),
        ).fetchall()
        after = self.db.execute(
            "SELECT data FROM events WHERE chat_key = ? AND kind = 'new' AND id > ? ORDER BY id LIMIT 2",
            (chat_key, origin),
        ).fetchall()
        return [self.decrypt(row[0])["after"] for row in [*reversed(before), *after]]

    def history(self, reference):
        rows = self.db.execute("""
            SELECT id, data FROM events
            WHERE message_key = (SELECT message_key FROM events WHERE id = ?)
            ORDER BY id
        """, (reference,))
        return [self.event(row) for row in rows]

    def event(self, row):
        return {**self.decrypt(row[1]), "ref": row[0]}

    def last_notice(self):
        row = self.db.execute(
            "SELECT id, data FROM events WHERE kind IN ('edit', 'delete') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return self.event(row) if row else None

    def recent(self):
        rows = self.db.execute("""
            SELECT id, data FROM events
            WHERE id IN (SELECT max(id) FROM events WHERE kind IN ('edit', 'delete', 'reply') GROUP BY message_key)
            ORDER BY id DESC LIMIT 10
        """)
        return [self.event(row) for row in rows]

    def queue_download(self, key, file_id, *, priority=False):
        self.db.execute("""
            INSERT INTO downloads(id, data, retry_at) VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET data = excluded.data, retry_at = excluded.retry_at
            WHERE excluded.retry_at < 0
        """, (key, self.encrypt(file_id), -1 if priority else 0))

    def next_download(self):
        row = self.db.execute(
            "SELECT id, data FROM downloads WHERE retry_at <= unixepoch() ORDER BY retry_at, rowid LIMIT 1"
        ).fetchone()
        return (row[0], self.decrypt(row[1])) if row else None

    def finish_download(self, key, file_id, retry_after=None):
        row = self.db.execute("SELECT data FROM downloads WHERE id = ?", (key,)).fetchone()
        if not row or self.decrypt(row[0]) != file_id:
            return
        if retry_after is None:
            self.db.execute("DELETE FROM downloads WHERE id = ? AND data = ?", (key, row[0]))
        else:
            self.db.execute("UPDATE downloads SET retry_at = unixepoch() + ? WHERE id = ? AND data = ?",
                            (retry_after, key, row[0]))

    def queue(self, method, parameters):
        payload = self.encrypt({"method": method, "parameters": parameters})
        self.db.execute("INSERT INTO outbox(data) VALUES (?)", (payload,))

    def pending(self):
        row = self.db.execute(
            "SELECT id, data FROM outbox WHERE ready_at <= ? ORDER BY id LIMIT 1", (time.time(),)
        ).fetchone()
        return (row[0], self.decrypt(row[1])) if row else None

    def has_pending(self):
        return self.db.execute("SELECT 1 FROM outbox LIMIT 1").fetchone() is not None

    def update_pending(self, item_id, method, parameters, delay=0):
        payload = self.encrypt({"method": method, "parameters": parameters})
        self.db.execute("UPDATE outbox SET data = ?, ready_at = ? WHERE id = ?",
                        (payload, time.time() + delay, item_id))

    def delivered(self, item_id):
        self.db.execute("DELETE FROM outbox WHERE id = ?", (item_id,))
