from __future__ import annotations

from collections import defaultdict, deque
from threading import Lock, RLock

from .domain import ModelMessage


class InMemoryConversationStore:
    """Development store. Replace with PostgreSQL without changing BotService."""

    def __init__(self, max_messages: int) -> None:
        self._max_messages = max_messages
        self._messages: dict[str, deque[ModelMessage]] = defaultdict(
            lambda: deque(maxlen=max_messages or None)
        )
        self._lock = RLock()
        self._conversation_locks: dict[str, Lock] = {}

    def conversation_lock(self, conversation_id: str) -> Lock:
        with self._lock:
            return self._conversation_locks.setdefault(conversation_id, Lock())

    def get(self, conversation_id: str) -> tuple[ModelMessage, ...]:
        if self._max_messages == 0:
            return ()
        with self._lock:
            return tuple(self._messages[conversation_id])

    def append_exchange(self, conversation_id: str, user: str, assistant: str) -> None:
        if self._max_messages == 0:
            return
        with self._lock:
            history = self._messages[conversation_id]
            history.append(ModelMessage(role="user", content=user))
            history.append(ModelMessage(role="assistant", content=assistant))

    def reset(self, conversation_id: str) -> None:
        with self._lock:
            self._messages.pop(conversation_id, None)


class MessageDeduplicator:
    """Tracks in-flight and completed message IDs with bounded memory."""

    def __init__(self, max_completed: int = 10_000) -> None:
        self._max_completed = max_completed
        self._in_flight: set[str] = set()
        self._completed: deque[str] = deque()
        self._completed_set: set[str] = set()
        self._lock = Lock()

    def acquire(self, message_id: str) -> bool:
        with self._lock:
            if message_id in self._in_flight or message_id in self._completed_set:
                return False
            self._in_flight.add(message_id)
            return True

    def complete(self, message_id: str) -> None:
        with self._lock:
            self._in_flight.discard(message_id)
            self._completed.append(message_id)
            self._completed_set.add(message_id)
            while len(self._completed) > self._max_completed:
                expired = self._completed.popleft()
                self._completed_set.discard(expired)

    def release(self, message_id: str) -> None:
        with self._lock:
            self._in_flight.discard(message_id)

