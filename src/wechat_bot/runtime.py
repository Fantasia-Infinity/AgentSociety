from __future__ import annotations

from collections import defaultdict, deque
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


class ActionOutbox:
    def __init__(self) -> None:
        self._actions: dict[str, deque[OutgoingAction]] = defaultdict(deque)
        self._condition = Condition()

    def push(self, action: OutgoingAction) -> None:
        with self._condition:
            self._actions[action.account_id].append(action)
            self._condition.notify_all()

    def poll(
        self,
        account_id: str,
        *,
        timeout: float,
        limit: int = 20,
    ) -> list[OutgoingAction]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._actions[account_id]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._condition.wait(timeout=remaining)
            actions = self._actions[account_id]
            return [actions.popleft() for _ in range(min(limit, len(actions)))]


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

    def poll_actions(self, account_id: str, *, timeout: float) -> list[OutgoingAction]:
        return self._outbox.poll(account_id, timeout=timeout)

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

