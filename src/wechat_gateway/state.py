from __future__ import annotations

from pathlib import Path
import sqlite3
import json
from threading import Lock
import time

from .domain import GatewayEvent


class GatewayInboxStore:
    """Durable Gateway inbox and per-chat recovery cursors."""

    def __init__(self, path: Path, *, retention_days: int = 30) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(path), check_same_thread=False, timeout=30
        )
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._lock = Lock()
        self._retention_seconds = retention_days * 24 * 60 * 60
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS inbox_messages (
                    message_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    lease_until REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    received_at REAL NOT NULL,
                    uploaded_at REAL
                )
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_inbox_pending
                ON inbox_messages(status, next_attempt_at, lease_until)
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_cursors (
                    account_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    cursor TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(account_id, chat_id)
                )
                """
            )

    def insert(self, event: GatewayEvent) -> bool:
        payload = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO inbox_messages(
                    message_id, account_id, chat_id, payload_json, received_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.message_id,
                    event.account_id,
                    event.chat_id,
                    payload,
                    time.time(),
                ),
            )
        return cursor.rowcount == 1

    def claim_pending(
        self, *, limit: int = 1, lease_seconds: float = 60
    ) -> list[GatewayEvent]:
        now = time.time()
        claimed: list[GatewayEvent] = []
        with self._lock, self._connection:
            rows = self._connection.execute(
                """
                SELECT message_id, payload_json
                FROM inbox_messages
                WHERE (
                    status = 'pending' AND next_attempt_at <= ?
                ) OR (
                    status = 'uploading' AND lease_until <= ?
                )
                ORDER BY received_at, message_id
                LIMIT ?
                """,
                (now, now, limit),
            ).fetchall()
            for message_id, payload_json in rows:
                self._connection.execute(
                    """
                    UPDATE inbox_messages
                    SET status='uploading', attempts=attempts+1,
                        lease_until=?, last_error=NULL
                    WHERE message_id=?
                    """,
                    (now + lease_seconds, message_id),
                )
                claimed.append(GatewayEvent.from_dict(json.loads(payload_json)))
        return claimed

    def mark_uploaded(self, message_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE inbox_messages
                SET status='uploaded', lease_until=0, uploaded_at=?, last_error=NULL
                WHERE message_id=?
                """,
                (time.time(), message_id),
            )

    def mark_rejected(self, message_id: str, reason: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE inbox_messages
                SET status='rejected', lease_until=0, last_error=?
                WHERE message_id=?
                """,
                (reason[:1000], message_id),
            )

    def retry(self, message_id: str, error: str, delay: float) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE inbox_messages
                SET status='pending', next_attempt_at=?, lease_until=0,
                    last_error=?
                WHERE message_id=?
                """,
                (time.time() + max(delay, 0), error[:1000], message_id),
            )

    def release_leases(self) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE inbox_messages
                SET status='pending', lease_until=0
                WHERE status='uploading'
                """
            )

    def pending_count(self) -> int:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COUNT(*) FROM inbox_messages
                WHERE status IN ('pending', 'uploading')
                """
            ).fetchone()
        return int(row[0]) if row else 0

    def status(self, message_id: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM inbox_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
        return None if row is None else str(row[0])

    def get_cursor(self, account_id: str, chat_id: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT cursor FROM chat_cursors
                WHERE account_id=? AND chat_id=?
                """,
                (account_id, chat_id),
            ).fetchone()
        return None if row is None else str(row[0])

    def set_cursor(self, account_id: str, chat_id: str, cursor: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO chat_cursors(account_id, chat_id, cursor, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(account_id, chat_id) DO UPDATE SET
                    cursor=excluded.cursor, updated_at=excluded.updated_at
                """,
                (account_id, chat_id, cursor, time.time()),
            )

    def close(self) -> None:
        with self._lock:
            try:
                self._connection.close()
            except sqlite3.ProgrammingError:
                pass


class SentActionStore:
    """Durable record used to suppress a resend when the HTTP ACK was lost."""

    def __init__(self, path: Path, *, retention_days: int = 30) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._lock = Lock()
        self._retention_seconds = retention_days * 24 * 60 * 60
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_actions (
                    action_id TEXT PRIMARY KEY,
                    sent_at INTEGER NOT NULL
                )
                """
            )
            self._connection.execute(
                "DELETE FROM sent_actions WHERE sent_at < ?",
                (int(time.time()) - self._retention_seconds,),
            )

    def was_sent(self, action_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM sent_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
        return row is not None

    def mark_sent(self, action_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO sent_actions(action_id, sent_at) VALUES (?, ?)",
                (action_id, int(time.time())),
            )

    def close(self) -> None:
        with self._lock:
            try:
                self._connection.close()
            except sqlite3.ProgrammingError:
                pass
