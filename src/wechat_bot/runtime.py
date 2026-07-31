from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import logging
from queue import Empty, Full, Queue
from threading import Condition, Event, Thread
import time

from .domain import IncomingMessage, OutgoingAction
from .service import BotService


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SubmitResult:
    accepted: bool
    reason: str


@dataclass(slots=True)
class _OutboxEntry:
    action: OutgoingAction
    leased_until: float = 0.0
    attempts: int = 0


class ActionOutbox:
    def __init__(self) -> None:
        self._actions: dict[str, dict[str, _OutboxEntry]] = defaultdict(dict)
        self._condition = Condition()

    def push(self, action: OutgoingAction) -> None:
        with self._condition:
            self._actions[action.account_id][action.action_id] = _OutboxEntry(action)
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
        with self._condition:
            while True:
                now = time.monotonic()
                available = [
                    entry
                    for entry in self._actions.get(account_id, {}).values()
                    if entry.leased_until <= now
                ]
                if available:
                    selected = available[:limit]
                    leased_until = now + lease_seconds
                    for entry in selected:
                        entry.leased_until = leased_until
                        entry.attempts += 1
                    return [entry.action for entry in selected]

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []

                leased = [
                    entry.leased_until
                    for entry in self._actions.get(account_id, {}).values()
                    if entry.leased_until > now
                ]
                wait_for = remaining
                if leased:
                    wait_for = min(wait_for, max(min(leased) - now, 0.001))
                self._condition.wait(timeout=wait_for)

    def ack(self, account_id: str, action_ids: list[str]) -> int:
        with self._condition:
            actions = self._actions.get(account_id)
            if not actions:
                return 0
            acked = 0
            for action_id in action_ids:
                if actions.pop(action_id, None) is not None:
                    acked += 1
            if not actions:
                self._actions.pop(account_id, None)
            self._condition.notify_all()
            return acked


class BotRuntime:
    def __init__(self, service: BotService, *, workers: int, queue_size: int) -> None:
        self._service = service
        self._queue: Queue[IncomingMessage] = Queue(maxsize=queue_size)
        self._outbox = ActionOutbox()
        self._stop = Event()
        self._threads = [
            Thread(target=self._worker, name=f"bot-worker-{index + 1}", daemon=True)
            for index in range(workers)
        ]

    def start(self) -> None:
        for thread in self._threads:
            thread.start()

    def submit(self, message: IncomingMessage) -> SubmitResult:
        try:
            self._queue.put_nowait(message)
        except Full:
            return SubmitResult(accepted=False, reason="queue_full")
        return SubmitResult(accepted=True, reason="queued")

    def poll_actions(
        self,
        account_id: str,
        *,
        timeout: float,
        lease_seconds: float = 30,
    ) -> list[OutgoingAction]:
        return self._outbox.poll(
            account_id,
            timeout=timeout,
            lease_seconds=lease_seconds,
        )

    def ack_actions(self, account_id: str, action_ids: list[str]) -> int:
        return self._outbox.ack(account_id, action_ids)

    def queue_depth(self) -> int:
        return self._queue.qsize()

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                message = self._queue.get(timeout=0.25)
            except Empty:
                continue
            try:
                result = self._service.handle(message)
                if result.action is not None:
                    self._outbox.push(result.action)
                logger.info(
                    "message_processed message_id=%s accepted=%s reason=%s",
                    message.message_id,
                    result.accepted,
                    result.reason,
                )
            except Exception:
                logger.exception("message_failed message_id=%s", message.message_id)
            finally:
                self._queue.task_done()
