from __future__ import annotations

from pathlib import Path
import sqlite3
from threading import Lock
import time


class SentActionStore:
    """Durable record used to suppress a resend when the HTTP ACK was lost."""

    def __init__(self, path: Path, *, retention_days: int = 30) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
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
            self._connection.close()
