from __future__ import annotations

import logging
from queue import Empty, Full, Queue
from threading import Event, Thread

from .adapter import WeChatAdapter
from .core_client import GatewayCoreClient, GatewayCoreRejectedError
from .domain import GatewayEvent
from .state import GatewayInboxStore, SentActionStore


logger = logging.getLogger(__name__)


class GatewayRuntime:
    def __init__(
        self,
        *,
        adapter: WeChatAdapter,
        client: GatewayCoreClient,
        sent_actions: SentActionStore,
        event_queue_size: int,
        poll_timeout_seconds: float,
        action_lease_seconds: float,
        retry_min_seconds: float,
        retry_max_seconds: float,
        inbox: GatewayInboxStore | None = None,
    ) -> None:
        self._adapter = adapter
        self._client = client
        self._sent_actions = sent_actions
        self._events: Queue[GatewayEvent] = Queue(maxsize=event_queue_size)
        self._poll_timeout = poll_timeout_seconds
        self._lease_seconds = action_lease_seconds
        self._retry_min = retry_min_seconds
        self._retry_max = retry_max_seconds
        self._inbox = inbox
        self._stop = Event()
        self._adapter_ready = Event()
        self._startup_error: Exception | None = None
        self._uploader = Thread(
            target=self._upload_loop,
            name="gateway-event-uploader",
            daemon=True,
        )
        self._actions = Thread(
            target=self._action_loop,
            name="gateway-wechat-actions",
            daemon=True,
        )
        self._started = False

    def start(self, *, startup_timeout_seconds: float = 60) -> None:
        if self._started:
            raise RuntimeError("gateway runtime has already been started")
        self._started = True
        self._uploader.start()
        self._actions.start()
        if not self._adapter_ready.wait(startup_timeout_seconds):
            self.stop()
            raise RuntimeError("WeChat adapter startup timed out")
        if self._startup_error is not None:
            self.stop()
            raise RuntimeError(f"WeChat adapter startup failed: {self._startup_error}")

    def stop(self) -> None:
        self._stop.set()
        if self._uploader.is_alive():
            self._uploader.join(timeout=5)
        if self._actions.is_alive():
            self._actions.join(timeout=max(self._poll_timeout + 6, 10))
        try:
            self._sent_actions.close()
        finally:
            if self._inbox is not None:
                self._inbox.release_leases()
                self._inbox.close()

    def event_queue_depth(self) -> int:
        if self._inbox is not None:
            return self._inbox.pending_count()
        return self._events.qsize()

    def _accept_event(self, event: GatewayEvent) -> bool:
        if self._inbox is not None:
            try:
                inserted = self._inbox.insert(event)
                source_key = str(event.metadata.get("source_key", "")).strip()
                if source_key:
                    self._inbox.set_cursor(
                        event.account_id, event.chat_id, source_key
                    )
            except Exception:
                logger.exception(
                    "gateway_event_persist_failed message_id=%s", event.message_id
                )
                return False
            if not inserted:
                logger.info(
                    "gateway_event_duplicate message_id=%s", event.message_id
                )
            return True
        try:
            self._events.put_nowait(event)
        except Full:
            logger.error(
                "gateway_event_queue_full message_id=%s chat_id=%s",
                event.message_id,
                event.chat_id,
            )
            return False
        return True

    def _upload_loop(self) -> None:
        if self._inbox is not None:
            self._durable_upload_loop()
            return
        while not self._stop.is_set():
            try:
                event = self._events.get(timeout=0.25)
            except Empty:
                continue
            delay = self._retry_min
            try:
                while not self._stop.is_set():
                    try:
                        self._client.submit_event(event)
                        logger.info(
                            "gateway_event_uploaded message_id=%s", event.message_id
                        )
                        break
                    except Exception:
                        logger.exception(
                            "gateway_event_upload_failed message_id=%s retry_in=%s",
                            event.message_id,
                            delay,
                        )
                        if self._stop.wait(delay):
                            break
                        delay = min(delay * 2, self._retry_max)
            finally:
                self._events.task_done()

    def _durable_upload_loop(self) -> None:
        assert self._inbox is not None
        while not self._stop.is_set():
            claimed = self._inbox.claim_pending(lease_seconds=max(self._retry_max, 60))
            if not claimed:
                self._stop.wait(0.25)
                continue
            event = claimed[0]
            delay = self._retry_min
            while not self._stop.is_set():
                try:
                    self._client.submit_event(event)
                    self._inbox.mark_uploaded(event.message_id)
                    logger.info(
                        "gateway_event_uploaded message_id=%s", event.message_id
                    )
                    break
                except GatewayCoreRejectedError as exc:
                    self._inbox.mark_rejected(event.message_id, str(exc))
                    logger.warning(
                        "gateway_event_rejected message_id=%s reason=%s",
                        event.message_id,
                        str(exc),
                    )
                    break
                except Exception as exc:
                    self._inbox.retry(event.message_id, str(exc), delay)
                    logger.exception(
                        "gateway_event_upload_failed message_id=%s retry_in=%s",
                        event.message_id,
                        delay,
                    )
                    if self._stop.wait(delay):
                        break
                    delay = min(delay * 2, self._retry_max)

    def _action_loop(self) -> None:
        try:
            self._adapter.start(self._accept_event)
        except Exception as exc:
            self._startup_error = exc
            self._adapter_ready.set()
            self._stop.set()
            try:
                self._adapter.stop()
            finally:
                self._sent_actions.close()
            return

        self._adapter_ready.set()
        delay = self._retry_min
        try:
            while not self._stop.is_set():
                try:
                    actions = self._client.poll_actions(
                        timeout_seconds=self._poll_timeout,
                        lease_seconds=self._lease_seconds,
                    )
                    delay = self._retry_min
                    for action in actions:
                        if self._stop.is_set():
                            break
                        if not self._sent_actions.was_sent(action.action_id):
                            self._adapter.send(action)
                            self._sent_actions.mark_sent(action.action_id)
                            logger.info(
                                "gateway_action_sent action_id=%s chat_id=%s",
                                action.action_id,
                                action.chat_id,
                            )
                        acked = self._client.ack_actions([action.action_id])
                        logger.info(
                            "gateway_action_acked action_id=%s acked=%s",
                            action.action_id,
                            acked,
                        )
                except Exception:
                    logger.exception("gateway_action_cycle_failed retry_in=%s", delay)
                    if self._stop.wait(delay):
                        break
                    delay = min(delay * 2, self._retry_max)
        finally:
            try:
                self._adapter.stop()
            finally:
                self._sent_actions.close()
