from __future__ import annotations

import json
import socket
from typing import Any, Protocol
import urllib.error
import urllib.parse
import urllib.request

from .domain import GatewayAction, GatewayEvent


class GatewayCoreClientError(RuntimeError):
    pass


class GatewayCoreRejectedError(GatewayCoreClientError):
    """Core accepted the HTTP request but permanently rejected the event."""


class JsonTransport(Protocol):
    def request_json(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> dict[str, Any]: ...


class UrllibJsonTransport:
    def request_json(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> dict[str, Any]:
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise GatewayCoreClientError(
                f"Bot Core HTTP {exc.code}: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise GatewayCoreClientError(f"Bot Core network error: {exc}") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GatewayCoreClientError("Bot Core returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise GatewayCoreClientError("Bot Core response must be a JSON object")
        return parsed


class GatewayCoreClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        account_id: str,
        timeout_seconds: float,
        transport: JsonTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._account_id = account_id
        self._timeout = timeout_seconds
        self._headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }
        self._transport = transport or UrllibJsonTransport()

    def submit_event(self, event: GatewayEvent) -> None:
        if event.account_id != self._account_id:
            raise GatewayCoreClientError("event account_id does not match gateway")
        response = self._transport.request_json(
            "POST",
            f"{self._base_url}/v1/events/wechat",
            self._headers,
            event.to_dict(),
            self._timeout,
        )
        if response.get("accepted") is not True:
            reason = str(response.get("reason") or "rejected")
            raise GatewayCoreRejectedError(f"Bot Core rejected event: {reason}")

    def poll_actions(
        self,
        *,
        timeout_seconds: float,
        lease_seconds: float,
    ) -> list[GatewayAction]:
        query = urllib.parse.urlencode(
            {
                "account_id": self._account_id,
                "timeout": timeout_seconds,
                "lease_seconds": lease_seconds,
            }
        )
        response = self._transport.request_json(
            "GET",
            f"{self._base_url}/v1/actions?{query}",
            self._headers,
            None,
            max(self._timeout, timeout_seconds + 5),
        )
        raw_actions = response.get("actions")
        if not isinstance(raw_actions, list):
            raise GatewayCoreClientError("Bot Core actions response is invalid")
        try:
            actions = [GatewayAction.from_dict(item) for item in raw_actions]
        except (TypeError, ValueError) as exc:
            raise GatewayCoreClientError(f"Bot Core returned an invalid action: {exc}") from exc
        if any(action.account_id != self._account_id for action in actions):
            raise GatewayCoreClientError("Bot Core returned an action for another account")
        return actions

    def ack_actions(self, action_ids: list[str]) -> int:
        if not action_ids:
            return 0
        response = self._transport.request_json(
            "POST",
            f"{self._base_url}/v1/actions/ack",
            self._headers,
            {"account_id": self._account_id, "action_ids": action_ids},
            self._timeout,
        )
        try:
            return int(response["acked"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GatewayCoreClientError("Bot Core ACK response is invalid") from exc
