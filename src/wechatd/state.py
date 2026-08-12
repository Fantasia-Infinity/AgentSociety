from __future__ import annotations

from pathlib import Path
import sqlite3
import json
from threading import Lock
import time

from .domain import GatewayEvent


class WechatdStore:
    """Durable message archive, per-chat recovery cursors, and agent read cursors.

    The wxauto adapter keeps its own UI recovery cursor (``chat_cursors``) so a
    re-login can replay messages the WeChat client still exposes. Agents read
    messages through ``agent_cursors``, which track the last message each
    agent-side consumer has seen per chat.
    """

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
                    status TEXT NOT NULL DEFAULT 'stored',
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
                CREATE INDEX IF NOT EXISTS idx_inbox_chat
                ON inbox_messages(account_id, chat_id, received_at)
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
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_cursors (
                    account_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    cursor TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(account_id, chat_id)
                )
                """
            )
            self._connection.execute(
                "DELETE FROM inbox_messages WHERE received_at < ?",
                (time.time() - self._retention_seconds,),
            )

    def store(self, event: GatewayEvent) -> bool:
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

    def messages_after(
        self,
        account_id: str,
        chat_id: str,
        *,
        after_message_id: str | None,
        before_timestamp: float | None = None,
        limit: int = 50,
    ) -> list[GatewayEvent]:
        with self._lock:
            if before_timestamp is not None:
                rows = self._connection.execute(
                    """
                    SELECT payload_json FROM inbox_messages
                    WHERE account_id=? AND chat_id=? AND received_at < ?
                    ORDER BY received_at DESC, message_id DESC
                    LIMIT ?
                    """,
                    (account_id, chat_id, before_timestamp, limit),
                ).fetchall()
                rows = list(reversed(rows))
            elif after_message_id:
                row = self._connection.execute(
                    """
                    SELECT received_at FROM inbox_messages WHERE message_id=?
                    """,
                    (after_message_id,),
                ).fetchone()
                if row is None:
                    return []
                anchor = float(row[0])
                rows = self._connection.execute(
                    """
                    SELECT payload_json FROM inbox_messages
                    WHERE account_id=? AND chat_id=?
                      AND (received_at > ? OR (received_at = ? AND message_id != ?))
                    ORDER BY received_at, message_id
                    LIMIT ?
                    """,
                    (account_id, chat_id, anchor, anchor, after_message_id, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT payload_json FROM inbox_messages
                    WHERE account_id=? AND chat_id=?
                    ORDER BY received_at, message_id
                    LIMIT ?
                    """,
                    (account_id, chat_id, limit),
                ).fetchall()
        return [GatewayEvent.from_dict(json.loads(str(row[0]))) for row in rows]

    def list_chats(
        self, account_id: str, *, limit: int = 100
    ) -> list[dict[str, object]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT chat_id,
                       MAX(received_at) AS last_at,
                       COUNT(*) AS total
                FROM inbox_messages
                WHERE account_id=?
                GROUP BY chat_id
                ORDER BY last_at DESC
                LIMIT ?
                """,
                (account_id, limit),
            ).fetchall()
        chats: list[dict[str, object]] = []
        for chat_id, last_at, total in rows:
            with self._lock:
                preview_row = self._connection.execute(
                    """
                    SELECT payload_json FROM inbox_messages
                    WHERE account_id=? AND chat_id=?
                    ORDER BY received_at DESC LIMIT 1
                    """,
                    (account_id, str(chat_id)),
                ).fetchone()
            preview = (
                GatewayEvent.from_dict(json.loads(str(preview_row[0])))
                if preview_row is not None
                else None
            )
            chats.append(
                {
                    "chat_id": str(chat_id),
                    "chat_type": preview.chat_type if preview else "direct",
                    "last_message_at": float(last_at),
                    "message_count": int(total),
                    "last_message_preview": (preview.content[:200] if preview else ""),
                }
            )
        return chats

    def get_message(self, account_id: str, message_id: str) -> GatewayEvent | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT payload_json FROM inbox_messages
                WHERE message_id=? AND account_id=?
                """,
                (message_id, account_id),
            ).fetchone()
        if row is None:
            return None
        return GatewayEvent.from_dict(json.loads(str(row[0])))

    def archive_depth(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM inbox_messages"
            ).fetchone()
        return int(row[0]) if row else 0

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

    def get_agent_cursor(self, account_id: str, chat_id: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT cursor FROM agent_cursors
                WHERE account_id=? AND chat_id=?
                """,
                (account_id, chat_id),
            ).fetchone()
        return None if row is None else str(row[0])

    def set_agent_cursor(self, account_id: str, chat_id: str, cursor: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO agent_cursors(account_id, chat_id, cursor, updated_at)
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
