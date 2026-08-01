from __future__ import annotations

from collections import deque
import hashlib
import importlib
import logging
from threading import Event, RLock, Thread
import time
from typing import Any, Callable

from ..adapter import EventCallback, WeChatAdapterError
from ..domain import GatewayAction, GatewayEvent


logger = logging.getLogger(__name__)


_CONTENT_TYPE_MAP = {
    "text": "text",
    "quote": "text",
    "image": "image",
    "file": "file",
    "voice": "audio",
    "video": "video",
}

_POLL_SEEN_LIMIT = 5_000
_POLL_CHAT_TIMEOUT_SECONDS = 3.0
_POLL_BASELINE_SNAPSHOTS = 3


class WxAutoAdapter:
    """Optional Windows adapter for wxauto 4.x free or Plus packages."""

    def __init__(
        self,
        *,
        account_id: str,
        module_name: str,
        listen_chats: tuple[str, ...],
        bot_mention: str,
        poll_interval_seconds: float = 1.0,
        poll_load_wait_seconds: float = 1.0,
        poll_baseline_seconds: float = 3.0,
        wechat_factory: Callable[[], Any] | None = None,
    ) -> None:
        if module_name not in {"wxauto4", "wxautox4"}:
            raise ValueError("unsupported wxauto module")
        if poll_interval_seconds <= 0:
            raise ValueError("wxauto poll interval must be positive")
        if poll_load_wait_seconds < 0:
            raise ValueError("wxauto poll load wait cannot be negative")
        if poll_baseline_seconds < 0:
            raise ValueError("wxauto poll baseline cannot be negative")
        self._account_id = account_id
        self._module_name = module_name
        self._listen_chats = listen_chats
        self._bot_mention = bot_mention
        self._poll_interval = poll_interval_seconds
        self._poll_load_wait = poll_load_wait_seconds
        self._poll_baseline = poll_baseline_seconds
        self._factory = wechat_factory
        self._wechat: Any | None = None
        self._on_event: EventCallback | None = None
        self._ui_lock = RLock()
        self._stop = Event()
        self._poller: Thread | None = None
        self._seen_message_ids: dict[str, set[str]] = {}
        self._seen_message_order: dict[str, deque[str]] = {}
        self._poll_snapshot_ready: dict[str, bool] = {}
        self._poll_session_markers: dict[str, str] = {}

    def start(self, on_event: EventCallback) -> None:
        self._stop.clear()
        self._seen_message_ids.clear()
        self._seen_message_order.clear()
        self._poll_snapshot_ready.clear()
        self._poll_session_markers.clear()
        if self._factory is None:
            try:
                module = importlib.import_module(self._module_name)
                if self._module_name == "wxauto4":
                    _install_navigation_alias()
                factory = module.WeChat
            except (ImportError, AttributeError) as exc:
                raise WeChatAdapterError(
                    f"Cannot load {self._module_name}; install it on Windows first"
                ) from exc
        else:
            factory = self._factory

        self._on_event = on_event
        try:
            if self._factory is None and self._module_name == "wxauto4":
                self._wechat = factory(ads=False)
            else:
                self._wechat = factory()
            add_listen_chat = getattr(self._wechat, "AddListenChat", None)
            if callable(add_listen_chat):
                self._start_callback_listener(add_listen_chat)
                mode = "callback"
            else:
                self._start_polling_listener()
                mode = "polling"
        except WeChatAdapterError:
            raise
        except Exception as exc:
            raise WeChatAdapterError(f"wxauto startup failed: {exc}") from exc
        logger.info(
            "wxauto_adapter_ready driver=%s mode=%s chats=%s",
            self._module_name,
            mode,
            len(self._listen_chats),
        )

    def send(self, action: GatewayAction) -> None:
        if action.content_type != "text":
            raise WeChatAdapterError(
                f"wxauto adapter cannot send content type: {action.content_type}"
            )
        if self._wechat is None:
            raise WeChatAdapterError("wxauto adapter has not been started")
        try:
            with self._ui_lock:
                result = self._wechat.SendMsg(
                    msg=action.content,
                    who=action.chat_id,
                    exact=True,
                )
        except Exception as exc:
            raise WeChatAdapterError(f"wxauto send failed: {exc}") from exc
        if result is not None and not bool(result):
            raise WeChatAdapterError(
                f"wxauto send failed: {_response_message(result)}"
            )

    def stop(self) -> None:
        self._stop.set()
        poller = self._poller
        self._poller = None
        if poller is not None and poller.is_alive():
            poller.join(timeout=max(self._poll_interval + 2, 5))
        wechat = self._wechat
        self._wechat = None
        self._on_event = None
        if wechat is None:
            return
        stop_listening = getattr(wechat, "StopListening", None)
        if callable(stop_listening):
            try:
                stop_listening()
            except Exception:
                logger.exception("wxauto_stop_failed")

    def _start_callback_listener(self, add_listen_chat: Callable[..., Any]) -> None:
        for nickname in self._listen_chats:
            result = add_listen_chat(
                nickname=nickname,
                callback=self._handle_message,
            )
            if result is not None and not bool(result):
                raise WeChatAdapterError(
                    f"wxauto could not listen to {nickname}: "
                    f"{_response_message(result)}"
                )

    def _start_polling_listener(self) -> None:
        wechat = self._wechat
        if wechat is None:
            raise WeChatAdapterError("wxauto adapter has not been started")
        if not callable(getattr(wechat, "ChatWith", None)) or not callable(
            getattr(wechat, "GetAllMessage", None)
        ):
            raise WeChatAdapterError(
                "installed wxauto package supports neither callback listening "
                "nor polling"
            )

        for nickname in self._listen_chats:
            self._baseline_chat(nickname)
        self._poller = Thread(
            target=self._poll_loop,
            name="wxauto-free-poller",
            daemon=True,
        )
        self._poller.start()

    def _poll_loop(self) -> None:
        while not self._stop.wait(self._poll_interval):
            for nickname in self._listen_chats:
                if self._stop.is_set():
                    return
                try:
                    self._poll_chat(nickname, emit_new=True)
                except Exception:
                    logger.exception("wxauto_poll_failed chat_id=%s", nickname)

    def _baseline_chat(self, nickname: str) -> None:
        deadline = time.monotonic() + self._poll_baseline
        snapshots = 0
        while True:
            self._poll_chat(nickname, emit_new=False)
            snapshots += 1
            if self._stop.is_set() or (
                snapshots >= _POLL_BASELINE_SNAPSHOTS
                and time.monotonic() >= deadline
            ):
                return

    def _poll_chat(self, nickname: str, *, emit_new: bool) -> None:
        wechat = self._wechat
        if wechat is None:
            return
        with self._ui_lock:
            snapshot_ready = self._poll_snapshot_ready.get(nickname, False)
            session_marker, session_new_count = _session_state(wechat, nickname)
            self._poll_session_markers.setdefault(nickname, session_marker)
            previous_marker = self._poll_session_markers.get(nickname, "")
            result = wechat.ChatWith(nickname, exact=True)
            if result is not None and not bool(result):
                raise WeChatAdapterError(
                    f"wxauto could not open {nickname}: {_response_message(result)}"
                )
            if self._stop.wait(self._poll_load_wait):
                return
            confirmed_chat = result.strip() if isinstance(result, str) else ""
            actual_chat = self._wait_for_chat(
                wechat,
                nickname,
                confirmed_chat=confirmed_chat,
            )
            if actual_chat is None:
                return
            messages = list(wechat.GetAllMessage())
            snapshot = _poll_snapshot(nickname, messages)
            if not snapshot_ready:
                for _, key in snapshot:
                    self._remember_polled_message(nickname, key)
                if messages:
                    self._poll_snapshot_ready[nickname] = True
                if (
                    emit_new
                    and messages
                    and previous_marker
                    and session_marker
                    and session_marker != previous_marker
                ):
                    deliverable = [
                        message
                        for message in messages
                        if _is_deliverable_message(message)
                    ]
                    count = max(session_new_count, 1)
                    for message in deliverable[-count:]:
                        self._handle_message(message, wechat)
                self._poll_session_markers[nickname] = session_marker
                return
            unseen = [
                message
                for message, key in snapshot
                if self._remember_polled_message(nickname, key)
            ]
            marker_available = bool(previous_marker and session_marker)
            session_changed = marker_available and session_marker != previous_marker
            if emit_new and unseen and (session_changed or not marker_available):
                deliverable = [
                    message for message in unseen if _is_deliverable_message(message)
                ]
                count = session_new_count if session_new_count > 0 else 1
                for message in deliverable[-count:]:
                    self._handle_message(message, wechat)
            self._poll_session_markers[nickname] = session_marker

    def _wait_for_chat(
        self,
        wechat: Any,
        nickname: str,
        *,
        confirmed_chat: str,
    ) -> str | None:
        deadline = time.monotonic() + _POLL_CHAT_TIMEOUT_SECONDS
        actual_chat = ""
        last_nonempty_chat = ""
        while not self._stop.is_set():
            actual_chat = str(_chat_info(wechat).get("chat_name", "")).strip()
            if actual_chat == nickname:
                return actual_chat
            if not actual_chat and confirmed_chat == nickname:
                return confirmed_chat
            if actual_chat:
                last_nonempty_chat = actual_chat
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if self._stop.wait(min(0.1, remaining)):
                return None
        raise WeChatAdapterError(
            f"wxauto opened unexpected chat: expected {nickname}, "
            f"got {last_nonempty_chat or '<unknown>'}"
        )

    def _remember_polled_message(self, chat_id: str, message_id: str) -> bool:
        seen = self._seen_message_ids.setdefault(chat_id, set())
        if message_id in seen:
            return False
        order = self._seen_message_order.setdefault(chat_id, deque())
        seen.add(message_id)
        order.append(message_id)
        if len(order) > _POLL_SEEN_LIMIT:
            seen.discard(order.popleft())
        return True

    def _handle_message(self, message: Any, chat: Any) -> None:
        callback = self._on_event
        if callback is None:
            return
        try:
            attr = str(getattr(message, "attr", "other"))
            if attr not in {"friend", "self"}:
                return
            raw_type = str(getattr(message, "type", "other"))
            content_type = _CONTENT_TYPE_MAP.get(raw_type)
            if content_type is None:
                return

            chat_info = _chat_info(chat)
            chat_id = str(
                chat_info.get("chat_name") or getattr(chat, "who", "")
            ).strip()
            if not chat_id:
                raise ValueError("wxauto callback did not include a chat name")
            raw_chat_type = str(
                chat_info.get("chat_type") or getattr(chat, "chat_type", "friend")
            )
            chat_type = "group" if raw_chat_type == "group" else "direct"
            sender_id = str(getattr(message, "sender", "") or chat_id).strip()
            content = str(getattr(message, "content", ""))
            timestamp = int(time.time())
            callback(
                GatewayEvent(
                    message_id=_message_id(
                        self._account_id,
                        message,
                        chat_id,
                        sender_id,
                        content,
                        timestamp,
                    ),
                    account_id=self._account_id,
                    chat_id=chat_id,
                    sender_id=sender_id,
                    chat_type=chat_type,
                    content_type=content_type,
                    content=content,
                    timestamp=timestamp,
                    mentioned_bot=(
                        chat_type == "group"
                        and bool(self._bot_mention)
                        and self._bot_mention in content
                    ),
                    is_self=attr == "self",
                    metadata={
                        "driver": self._module_name,
                        "wxauto_attr": attr,
                        "wxauto_type": raw_type,
                        "wxauto_chat_type": raw_chat_type,
                    },
                )
            )
        except Exception:
            logger.exception("wxauto_message_normalization_failed")


def _chat_info(chat: Any) -> dict[str, Any]:
    method = getattr(chat, "ChatInfo", None)
    if not callable(method):
        return {}
    value = method()
    return value if isinstance(value, dict) else {}


def _poll_snapshot(
    chat_id: str,
    messages: list[Any],
) -> list[tuple[Any, str]]:
    occurrences: dict[str, int] = {}
    snapshot = []
    for message in messages:
        fingerprint = _poll_message_fingerprint(chat_id, message)
        occurrence = occurrences.get(fingerprint, 0)
        occurrences[fingerprint] = occurrence + 1
        snapshot.append((message, f"{fingerprint}:{occurrence}"))
    return snapshot


def _poll_message_fingerprint(chat_id: str, message: Any) -> str:
    raw = "\0".join(
        (
            chat_id,
            type(message).__name__,
            str(getattr(message, "attr", "")),
            str(getattr(message, "type", "")),
            str(getattr(message, "sender", "")),
            str(getattr(message, "content", "")),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_deliverable_message(message: Any) -> bool:
    return (
        str(getattr(message, "attr", "other")) in {"friend", "self"}
        and str(getattr(message, "type", "other")) in _CONTENT_TYPE_MAP
    )


def _session_state(wechat: Any, nickname: str) -> tuple[str, int]:
    get_sessions = getattr(wechat, "GetSession", None)
    if not callable(get_sessions):
        return "", 0
    for session in get_sessions() or ():
        name = str(
            getattr(session, "name", "") or getattr(session, "who", "")
        ).strip()
        if name != nickname:
            continue
        raw_count = getattr(session, "new_count", 0)
        try:
            new_count = int(raw_count)
        except (TypeError, ValueError):
            new_count = 0
        raw = "\0".join(
            (
                name,
                str(getattr(session, "content", "")),
                str(getattr(session, "time", "")),
            )
        )
        marker = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return marker, max(new_count, 0)
    return "", 0


def _message_id(
    account_id: str,
    message: Any,
    chat_id: str,
    sender_id: str,
    content: str,
    timestamp: int,
) -> str:
    for name in ("hash", "id"):
        value = getattr(message, name, None)
        if value is not None and not callable(value) and str(value).strip():
            return f"wxauto:{account_id}:{value}"
    raw = "\0".join((account_id, chat_id, sender_id, content, str(timestamp)))
    return "wxauto:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _response_message(response: Any) -> str:
    try:
        return str(response["message"])
    except (KeyError, TypeError):
        return str(response)


class _ControlNameAlias:
    def __init__(self, control: Any, name: str) -> None:
        self._control = control
        self.Name = name

    def __getattr__(self, name: str) -> Any:
        return getattr(self._control, name)


def _install_navigation_alias(uiautomation: Any | None = None) -> None:
    if uiautomation is None:
        uiautomation = importlib.import_module("wxauto4.uia.uiautomation")
    control_class = uiautomation.Control
    original = control_class._CompareFunction
    if getattr(original, "_wechat_gateway_navigation_alias", False):
        return

    def compare_with_alias(self: Any, control: Any, depth: int) -> bool:
        properties = self.searchProperties
        if (
            properties.get("ClassName") == "mmui::XTabBarItem"
            and properties.get("Name") == "微信"
            and getattr(control, "ClassName", "") == "mmui::XTabBarItem"
            and getattr(control, "Name", "") == "WeChat"
        ):
            return original(self, _ControlNameAlias(control, "微信"), depth)
        return original(self, control, depth)

    compare_with_alias._wechat_gateway_navigation_alias = True  # type: ignore[attr-defined]
    control_class._CompareFunction = compare_with_alias
