"""État persistant (SQLite) : dernier état de chaque modèle/site, santé des connecteurs,
emails déjà traités, horodatage du dernier appel. Survit aux redémarrages."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import ItemState

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    key TEXT PRIMARY KEY,
    connector TEXT NOT NULL,
    model_id TEXT NOT NULL,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS health (
    connector TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    incident_open INTEGER NOT NULL DEFAULT 0,
    last_success TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS emails (
    message_id TEXT PRIMARY KEY,
    result TEXT NOT NULL,
    processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


@dataclass
class Health:
    connector: str
    state: str = "OK"  # OK | DÉGRADÉ | DÉSACTIVÉ | EN ATTENTE
    consecutive_failures: int = 0
    incident_open: bool = False
    last_success: str | None = None
    last_error: str | None = None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class StateStore:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

    # --- items -------------------------------------------------------------
    def get_item(self, key: str) -> ItemState | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM items WHERE key=?", (key,)).fetchone()
        return ItemState.from_json(row[0]) if row else None

    def put_item(self, item: ItemState) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO items(key, connector, model_id, data, updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (item.key, item.connector, item.model_id, item.to_json(), _now()),
            )
            self._db.commit()

    def items(self, connector: str | None = None) -> list[ItemState]:
        with self._lock:
            if connector:
                rows = self._db.execute("SELECT data FROM items WHERE connector=? ORDER BY key", (connector,)).fetchall()
            else:
                rows = self._db.execute("SELECT data FROM items ORDER BY key").fetchall()
        return [ItemState.from_json(r[0]) for r in rows]

    # --- santé des connecteurs --------------------------------------------
    def get_health(self, connector: str) -> Health:
        with self._lock:
            row = self._db.execute(
                "SELECT state, consecutive_failures, incident_open, last_success, last_error FROM health WHERE connector=?",
                (connector,),
            ).fetchone()
        if not row:
            return Health(connector=connector, state="EN ATTENTE")
        return Health(connector, row[0], row[1], bool(row[2]), row[3], row[4])

    def put_health(self, h: Health) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO health(connector, state, consecutive_failures, incident_open, last_success, last_error) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(connector) DO UPDATE SET state=excluded.state, "
                "consecutive_failures=excluded.consecutive_failures, incident_open=excluded.incident_open, "
                "last_success=excluded.last_success, last_error=excluded.last_error",
                (h.connector, h.state, h.consecutive_failures, int(h.incident_open), h.last_success, h.last_error),
            )
            self._db.commit()

    # --- emails ------------------------------------------------------------
    def email_result(self, message_id: str) -> str | None:
        """None si jamais vu, sinon « alert » ou « ignored »."""
        with self._lock:
            row = self._db.execute("SELECT result FROM emails WHERE message_id=?", (message_id,)).fetchone()
        return row[0] if row else None

    def mark_email(self, message_id: str, result: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO emails(message_id, result, processed_at) VALUES(?,?,?)",
                (message_id, result, _now()),
            )
            self._db.commit()

    # --- divers ------------------------------------------------------------
    def get_meta(self, k: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else None

    def set_meta(self, k: str, v: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?,?)", (k, v))
            self._db.commit()
