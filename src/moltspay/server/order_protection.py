"""Bounded process-local admission control for payment-order creation."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Iterator, Optional


class OrderProtectionError(RuntimeError):
    """A request was rejected before signing or contacting a payment provider."""

    def __init__(self, code: str, message: str, retry_after: int = 1):
        super().__init__(message)
        self.code = code
        self.retry_after = max(1, int(retry_after))


class OrderCapacityError(OrderProtectionError):
    """The durable order store has reached its unpaid-order ceiling."""


@dataclass
class _Bucket:
    tokens: float
    updated: float
    active: int = 0
    last_seen: float = 0.0


def _number(config: Dict[str, object], names: tuple[str, ...], default: float) -> float:
    for name in names:
        if name in config and config[name] is not None:
            try:
                value = float(config[name])
            except (TypeError, ValueError):
                raise ValueError(f"{name} must be numeric")
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")
            return value
    return default


def _integer(config: Dict[str, object], names: tuple[str, ...], default: int) -> int:
    value = int(_number(config, names, default))
    if value <= 0:
        raise ValueError(f"{names[0]} must be greater than zero")
    return value


class OrderCreationLimiter:
    """A bounded, thread-safe global and per-caller order admission limiter.

    This is deliberately only a process-local fast gate.  Durable ceilings are
    enforced by each SQLite order store, so restarting a process cannot reset
    the unpaid-order budget.
    """

    def __init__(self, config: Optional[Dict[str, object]] = None):
        config = dict(config or {})
        self.global_rate = _number(config, ("global_rate_per_second", "order_rate_per_second", "max_orders_per_second"), 10.0)
        self.global_burst = _number(config, ("global_burst", "order_burst", "max_order_burst"), max(1.0, self.global_rate * 2))
        self.caller_rate = _number(config, ("caller_rate_per_second", "per_caller_rate_per_second"), 5.0)
        self.caller_burst = _number(config, ("caller_burst", "per_caller_burst"), max(1.0, self.caller_rate * 2))
        self.max_global_concurrent = _integer(config, ("max_concurrent_order_creations", "max_concurrent_orders"), 8)
        self.max_caller_concurrent = _integer(config, ("max_concurrent_orders_per_caller", "max_concurrent_per_caller"), 2)
        self.max_callers = _integer(config, ("max_tracked_callers",), 4096)
        self.caller_ttl = _number(config, ("caller_state_ttl_seconds",), 300.0)
        now = time.monotonic()
        self._global = _Bucket(self.global_burst, now, last_seen=now)
        self._callers: "OrderedDict[str, _Bucket]" = OrderedDict()
        self._global_active = 0
        self._lock = threading.RLock()

    @staticmethod
    def _refill(bucket: _Bucket, rate: float, burst: float, now: float) -> None:
        elapsed = max(0.0, now - bucket.updated)
        bucket.tokens = min(burst, bucket.tokens + elapsed * rate)
        bucket.updated = now
        bucket.last_seen = now

    def _prune(self, now: float) -> None:
        stale = [key for key, bucket in self._callers.items()
                 if bucket.active == 0 and now - bucket.last_seen > self.caller_ttl]
        for key in stale:
            self._callers.pop(key, None)
        while len(self._callers) >= self.max_callers:
            key, bucket = next(iter(self._callers.items()))
            if bucket.active:
                break
            self._callers.pop(key, None)

    @contextmanager
    def slot(self, caller_id: str) -> Iterator[None]:
        caller_id = str(caller_id or "unknown")
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            bucket = self._callers.get(caller_id)
            if bucket is None:
                if len(self._callers) >= self.max_callers:
                    raise OrderProtectionError("caller_limit", "too many active order callers", 5)
                bucket = _Bucket(self.caller_burst, now, last_seen=now)
                self._callers[caller_id] = bucket
            else:
                self._callers.move_to_end(caller_id)
            self._refill(self._global, self.global_rate, self.global_burst, now)
            self._refill(bucket, self.caller_rate, self.caller_burst, now)
            if self._global_active >= self.max_global_concurrent:
                raise OrderProtectionError("order_concurrency_limit", "order creation concurrency limit reached", 1)
            if bucket.active >= self.max_caller_concurrent:
                raise OrderProtectionError("caller_concurrency_limit", "caller order concurrency limit reached", 1)
            if self._global.tokens < 1:
                raise OrderProtectionError("order_rate_limit", "order creation rate limit reached", 1)
            if bucket.tokens < 1:
                raise OrderProtectionError("caller_rate_limit", "caller order rate limit reached", 1)
            self._global.tokens -= 1
            bucket.tokens -= 1
            self._global_active += 1
            bucket.active += 1
        try:
            yield
        finally:
            with self._lock:
                self._global_active = max(0, self._global_active - 1)
                bucket = self._callers.get(caller_id)
                if bucket is not None:
                    bucket.active = max(0, bucket.active - 1)
                    bucket.last_seen = time.monotonic()


__all__ = ["OrderCapacityError", "OrderCreationLimiter", "OrderProtectionError"]
