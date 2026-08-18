"""Outbound DSH Web tunnel registry (in-memory, thread-safe).

Stage-one tunnel transport: a device opens an outbound WebSocket to the Hub
(so NAT/firewall is fine), proving possession of a short-lived one-time
ticket. The Hub then routes browser requests for that node's `dsh web` over
the attached connection. Nothing here ever dials the device.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Callable

TUNNEL_TICKET_TTL_SECONDS = 120

SendFn = Callable[[dict[str, Any]], None]
CloseFn = Callable[[], None]


class TunnelRegistry:
    """Ticket issuance plus one active outbound tunnel per node."""

    def __init__(self, ticket_ttl_seconds: int = TUNNEL_TICKET_TTL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._ticket_ttl = ticket_ttl_seconds
        # ticket -> {"node_id": str, "expires_at": float}
        self._tickets: dict[str, dict[str, Any]] = {}
        # node_id -> {"send": SendFn, "close": CloseFn}
        self._tunnels: dict[str, dict[str, Any]] = {}

    @property
    def ticket_ttl_seconds(self) -> int:
        return self._ticket_ttl

    def issue_ticket(self, node_id: str) -> str:
        ticket = secrets.token_urlsafe(32)
        with self._lock:
            self._tickets[ticket] = {
                "node_id": node_id,
                "expires_at": time.time() + self._ticket_ttl,
            }
        return ticket

    def consume_ticket(self, ticket: str) -> str | None:
        """Validate and one-time-consume a ticket, returning the node id."""
        with self._lock:
            row = self._tickets.pop(ticket, None)
            if row is None:
                return None
            if row["expires_at"] < time.time():
                return None
            return row["node_id"]

    def attach(self, node_id: str, send: SendFn, close: CloseFn) -> None:
        """Register the node's active tunnel; any previous tunnel is closed."""
        with self._lock:
            previous = self._tunnels.pop(node_id, None)
            self._tunnels[node_id] = {"send": send, "close": close}
        if previous is not None:
            try:
                previous["close"]()
            except Exception:
                pass

    def detach(self, node_id: str, close: CloseFn) -> None:
        with self._lock:
            current = self._tunnels.get(node_id)
            if current is not None and current["close"] is close:
                self._tunnels.pop(node_id, None)

    def send_to(self, node_id: str, message: dict[str, Any]) -> bool:
        """Send one JSON message to the node's tunnel; False if offline."""
        with self._lock:
            tunnel = self._tunnels.get(node_id)
            send = tunnel["send"] if tunnel is not None else None
        if send is None:
            return False
        try:
            send(message)
        except Exception:
            return False
        return True

    def is_online(self, node_id: str) -> bool:
        with self._lock:
            return node_id in self._tunnels
