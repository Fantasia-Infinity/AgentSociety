from __future__ import annotations

from collections import deque
import threading
import time


class SlidingWindowLimiter:
    """In-memory sliding-window rate limiter keyed by an arbitrary string."""

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        clock=time.monotonic,
    ) -> None:
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("limit and window_seconds must be positive")
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = {}
        self._last_sweep = clock()

    def _sweep(self, now: float) -> None:
        stale = [
            key
            for key, events in self._events.items()
            if not events or now - events[-1] >= self._window
        ]
        for key in stale:
            self._events.pop(key, None)
        self._last_sweep = now

    def allow(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            if now - self._last_sweep >= self._window:
                self._sweep(now)
            events = self._events.get(key)
            if events is None:
                self._events[key] = deque([now])
                return True
            while events and now - events[0] >= self._window:
                events.popleft()
            if not events:
                self._events.pop(key, None)
                self._events[key] = deque([now])
                return True
            if len(events) >= self._limit:
                return False
            events.append(now)
            return True


class AuthRateLimiter:
    """Per-source-IP throttling for authentication endpoints."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        auth_per_minute: int = 20,
        register_per_hour: int = 10,
    ) -> None:
        self.enabled = enabled
        self._auth = SlidingWindowLimiter(auth_per_minute, 60)
        self._register = SlidingWindowLimiter(register_per_hour, 3600)

    def allow_auth(self, ip: str) -> bool:
        if not self.enabled:
            return True
        return self._auth.allow(ip)

    def allow_register(self, ip: str) -> bool:
        if not self.enabled:
            return True
        return self._auth.allow(ip) and self._register.allow(ip)
