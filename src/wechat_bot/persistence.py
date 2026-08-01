from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import sqlite3
from threading import Condition, Lock, RLock
import time
import uuid

from .domain import IncomingMessage, ModelMessage, OutgoingAction


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


class CoreInboxStore:
    """Durable Core inbox; accepted HTTP events survive Core restarts."""

    def __init__(self, path: Path) -> None:
        self._connection = _connect(path)
        self._lock = Lock()
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS core_inbox (
                    message_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    lease_until REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    received_at REAL NOT NULL,
                    completed_at REAL,
                    reason TEXT
                )
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_core_inbox_pending
                ON core_inbox(status, next_attempt_at, lease_until)
                """
            )

    def insert(self, message: IncomingMessage) -> bool:
        payload = json.dumps(message_to_dict(message), ensure_ascii=False, sort_keys=True)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO core_inbox(message_id, payload_json, received_at)
                VALUES (?, ?, ?)
                """,
                (message.message_id, payload, time.time()),
            )
        return cursor.rowcount == 1

    def claim_pending(
        self, *, limit: int = 1, lease_seconds: float = 120
    ) -> list[tuple[IncomingMessage, int]]:
        now = time.time()
        claimed: list[tuple[IncomingMessage, int]] = []
        with self._lock, self._connection:
            rows = self._connection.execute(
                """
                SELECT message_id, payload_json, attempts
                FROM core_inbox
                WHERE (
                    status='pending' AND next_attempt_at <= ?
                ) OR (
                    status='processing' AND lease_until <= ?
                )
                ORDER BY received_at, message_id
                LIMIT ?
                """,
                (now, now, limit),
            ).fetchall()
            for message_id, payload_json, attempts in rows:
                next_attempt = int(attempts) + 1
                self._connection.execute(
                    """
                    UPDATE core_inbox
                    SET status='processing', attempts=?, lease_until=?, last_error=NULL
                    WHERE message_id=?
                    """,
                    (next_attempt, now + lease_seconds, message_id),
                )
                claimed.append(
                    (IncomingMessage.from_dict(json.loads(payload_json)), next_attempt)
                )
        return claimed

    def mark_completed(self, message_id: str, reason: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE core_inbox
                SET status='completed', lease_until=0, completed_at=?, reason=?
                WHERE message_id=?
                """,
                (time.time(), reason[:200], message_id),
            )

    def retry(self, message_id: str, error: str, delay: float) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE core_inbox
                SET status='pending', lease_until=0, next_attempt_at=?, last_error=?
                WHERE message_id=?
                """,
                (time.time() + max(delay, 0), error[:1000], message_id),
            )

    def release_leases(self) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE core_inbox
                SET status='pending', lease_until=0
                WHERE status='processing'
                """
            )

    def queue_depth(self) -> int:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COUNT(*) FROM core_inbox
                WHERE status IN ('pending', 'processing')
                """
            ).fetchone()
        return int(row[0]) if row else 0

    def status(self, message_id: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM core_inbox WHERE message_id=?",
                (message_id,),
            ).fetchone()
        return None if row is None else str(row[0])

    def close(self) -> None:
        with self._lock:
            try:
                self._connection.close()
            except sqlite3.ProgrammingError:
                pass


def message_to_dict(message: IncomingMessage) -> dict[str, object]:
    return {
        "message_id": message.message_id,
        "account_id": message.account_id,
        "chat_id": message.chat_id,
        "sender_id": message.sender_id,
        "chat_type": message.chat_type.value,
        "content_type": message.content_type.value,
        "content": message.content,
        "timestamp": message.timestamp,
        "mentioned_bot": message.mentioned_bot,
        "is_self": message.is_self,
        "metadata": message.metadata,
    }


class SqliteConversationStore:
    """Conversation history that survives Core restarts."""

    def __init__(self, path: Path, max_messages: int) -> None:
        self._connection = _connect(path)
        self._max_messages = max_messages
        self._lock = RLock()
        self._conversation_locks: dict[str, Lock] = defaultdict(Lock)
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT UNIQUE,
                    conversation_id TEXT NOT NULL,
                    user_content TEXT NOT NULL,
                    assistant_content TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )

    def conversation_lock(self, conversation_id: str) -> Lock:
        with self._lock:
            return self._conversation_locks[conversation_id]

    def get(self, conversation_id: str) -> tuple[ModelMessage, ...]:
        if self._max_messages == 0:
            return ()
        turns = max(self._max_messages // 2, 1)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT user_content, assistant_content
                FROM conversation_turns
                WHERE conversation_id=?
                ORDER BY seq DESC
                LIMIT ?
                """,
                (conversation_id, turns),
            ).fetchall()
        messages: list[ModelMessage] = []
        for user_content, assistant_content in reversed(rows):
            messages.extend(
                (
                    ModelMessage(role="user", content=str(user_content)),
                    ModelMessage(role="assistant", content=str(assistant_content)),
                )
            )
        return tuple(messages[-self._max_messages :])

    def append_exchange(
        self,
        conversation_id: str,
        user: str,
        assistant: str,
        *,
        message_id: str | None = None,
    ) -> None:
        key = message_id or str(uuid.uuid4())
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO conversation_turns(
                    message_id, conversation_id, user_content, assistant_content, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (key, conversation_id, user, assistant, time.time()),
            )

    def close(self) -> None:
        with self._lock:
            try:
                self._connection.close()
            except sqlite3.ProgrammingError:
                pass


class SqliteMessageDeduplicator:
    """Persistent message completion state with an expiring processing lease."""

    def __init__(self, path: Path, *, lease_seconds: float = 120) -> None:
        self._connection = _connect(path)
        self._lock = Lock()
        self._lease_seconds = lease_seconds
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    lease_until REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                )
                """
            )

    def acquire(self, message_id: str) -> bool:
        now = time.time()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT status, lease_until FROM processed_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if row is not None:
                status, lease_until = str(row[0]), float(row[1])
                if status == "completed" or (status == "processing" and lease_until > now):
                    return False
            self._connection.execute(
                """
                INSERT INTO processed_messages(message_id, status, lease_until, updated_at)
                VALUES (?, 'processing', ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    status='processing', lease_until=excluded.lease_until,
                    updated_at=excluded.updated_at
                """,
                (message_id, now + self._lease_seconds, now),
            )
        return True

    def complete(self, message_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE processed_messages
                SET status='completed', lease_until=0, updated_at=?
                WHERE message_id=?
                """,
                (time.time(), message_id),
            )

    def release(self, message_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM processed_messages WHERE message_id=? AND status='processing'",
                (message_id,),
            )

    def close(self) -> None:
        with self._lock:
            try:
                self._connection.close()
            except sqlite3.ProgrammingError:
                pass


class SqliteActionOutbox:
    """Durable Core reply outbox with the same lease/ACK contract."""

    def __init__(self, path: Path) -> None:
        self._connection = _connect(path)
        self._lock = Lock()
        self._condition = Condition(self._lock)
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS core_actions (
                    action_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    leased_until REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_core_actions_account ON core_actions(account_id, leased_until)"
            )

    def push(self, action: OutgoingAction) -> None:
        payload = json.dumps(action.to_dict(), ensure_ascii=False, sort_keys=True)
        with self._condition, self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO core_actions(
                    action_id, account_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (action.action_id, action.account_id, payload, time.time()),
            )
            self._condition.notify_all()

    def poll(
        self,
        account_id: str,
        *,
        timeout: float,
        limit: int = 20,
        lease_seconds: float = 30,
    ) -> list[OutgoingAction]:
        deadline = time.monotonic() + timeout
        while True:
            now = time.time()
            with self._condition, self._connection:
                rows = self._connection.execute(
                    """
                    SELECT action_id, payload_json
                    FROM core_actions
                    WHERE account_id=? AND leased_until <= ?
                    ORDER BY created_at, action_id
                    LIMIT ?
                    """,
                    (account_id, now, limit),
                ).fetchall()
                if rows:
                    ids = [str(row[0]) for row in rows]
                    placeholders = ",".join("?" for _ in ids)
                    self._connection.execute(
                        f"UPDATE core_actions SET leased_until=?, attempts=attempts+1 WHERE action_id IN ({placeholders})",
                        (now + lease_seconds, *ids),
                    )
                    return [
                        OutgoingAction.from_dict(json.loads(row[1])) for row in rows
                    ]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            with self._condition:
                self._condition.wait(timeout=min(remaining, 0.25))

    def ack(self, account_id: str, action_ids: list[str]) -> int:
        if not action_ids:
            return 0
        with self._condition, self._connection:
            placeholders = ",".join("?" for _ in action_ids)
            cursor = self._connection.execute(
                f"DELETE FROM core_actions WHERE account_id=? AND action_id IN ({placeholders})",
                (account_id, *action_ids),
            )
            self._condition.notify_all()
        return cursor.rowcount

    def close(self) -> None:
        with self._condition:
            try:
                self._connection.close()
            except sqlite3.ProgrammingError:
                pass
