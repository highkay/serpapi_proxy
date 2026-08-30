"""SQLite key store for the SerpApi pool. Stdlib ``sqlite3`` only.

Connections are opened per operation (same pattern as the harvester's
``web/serpapi_push.py`` push-log writer), so the store is safe to share
between the API handlers and the refresher thread. Keys are stored
lowercased; the ``api_key`` field echoed by account.json is never persisted.
"""

from __future__ import annotations

import os
import sqlite3
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL UNIQUE,
  alias TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'unverified',
  plan_name TEXT,
  plan_id TEXT,
  searches_left INTEGER,
  renewal_date TEXT,
  added_at REAL NOT NULL,
  last_used_at REAL,
  last_check_at REAL,
  cooldown_until REAL NOT NULL DEFAULT 0.0
)
"""


class KeyStore:
    """Per-call-connection SQLite store for pooled SerpApi keys."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _update(self, statement: str, params: tuple[object, ...]) -> None:
        conn = self._connect()
        try:
            conn.execute(statement, params)
            conn.commit()
        finally:
            conn.close()

    def add(self, key: str, alias: str) -> int:
        """Insert a new key; returns its id. Raises IntegrityError on dup."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO keys (key, alias, added_at) VALUES (?, ?, ?)",
                (key.lower(), alias, time.time()),
            )
            row_id = cur.lastrowid
            conn.commit()
            assert row_id is not None  # fresh AUTOINCREMENT insert
            return int(row_id)
        finally:
            conn.close()

    def find_by_key(self, key: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM keys WHERE key = ?", (key.lower(),)
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def get(self, key_id: int) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM keys WHERE id = ?", (key_id,)
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def list(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM keys ORDER BY id").fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def delete(self, key_id: int) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute("DELETE FROM keys WHERE id = ?", (key_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def pick(self, now: float) -> dict | None:
        """Pick the best rotatable key: unknown quota first, then most
        searches left, then least recently used."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM keys
                WHERE status IN ('active', 'unverified')
                  AND cooldown_until <= ?
                  AND (searches_left IS NULL OR searches_left > 0)
                ORDER BY (searches_left IS NULL) DESC,
                         searches_left DESC,
                         COALESCE(last_used_at, 0) ASC,
                         id ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def mark_used(self, key_id: int, now: float) -> None:
        self._update(
            "UPDATE keys SET last_used_at = ? WHERE id = ?", (now, key_id)
        )

    def set_cooldown(self, key_id: int, until: float) -> None:
        self._update(
            "UPDATE keys SET cooldown_until = ? WHERE id = ?", (until, key_id)
        )

    def set_status(self, key_id: int, status: str) -> None:
        self._update(
            "UPDATE keys SET status = ? WHERE id = ?", (status, key_id)
        )

    def update_account(
        self,
        key_id: int,
        *,
        status: str,
        plan_name: str | None,
        plan_id: str | None,
        searches_left: int | None,
        renewal_date: str | None,
        now: float,
    ) -> None:
        """Apply an account.json verdict to a stored key."""
        self._update(
            """
            UPDATE keys SET status = ?, plan_name = ?, plan_id = ?,
                   searches_left = ?, renewal_date = ?, last_check_at = ?
            WHERE id = ?
            """,
            (
                status,
                plan_name,
                plan_id,
                searches_left,
                renewal_date,
                now,
                key_id,
            ),
        )

    def counts(self) -> dict:
        conn = self._connect()
        try:
            total = conn.execute("SELECT COUNT(*) FROM keys").fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM keys WHERE status = 'active'"
            ).fetchone()[0]
            return {"keys": int(total), "active": int(active)}
        finally:
            conn.close()

    def ids_checkable(self) -> list[int]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id FROM keys WHERE status != 'invalid' ORDER BY id"
            ).fetchall()
            return [int(row[0]) for row in rows]
        finally:
            conn.close()