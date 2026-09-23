"""HTTP access for collectors: per-key rate limiting + retries with backoff and jitter."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

log = structlog.get_logger()

RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass
class RateLimiter:
    """Sliding window: at most `calls` acquisitions per `period` seconds. Thread-safe."""

    calls: int
    period: float
    _stamps: deque[float] = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._stamps and now - self._stamps[0] >= self.period:
                    self._stamps.popleft()
                if len(self._stamps) < self.calls:
                    self._stamps.append(now)
                    return
                wait = self.period - (now - self._stamps[0])
            time.sleep(wait)


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRY_STATUSES
    return isinstance(exc, httpx.TransportError)


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


def make_getter(
    client: httpx.Client,
    limiter: RateLimiter,
    *,
    source: str,
    max_attempts: int = 5,
    max_wait: float = 120,
):
    """Returns get(url, **kwargs) -> httpx.Response that is rate limited and retried."""

    @retry(
        retry=retry_if_exception(_retryable),
        wait=wait_random_exponential(multiplier=1, max=max_wait),
        stop=stop_after_attempt(max_attempts),
        reraise=True,
    )
    def get(url: str, **kwargs: Any) -> httpx.Response:
        limiter.acquire()
        resp = client.get(url, **kwargs)
        if resp.status_code in RETRY_STATUSES:
            log.warning("http_retryable_status", source=source, url=url, status=resp.status_code)
            if (delay := _retry_after(resp)) is not None:
                time.sleep(min(delay, max_wait))
        resp.raise_for_status()
        return resp

    return get
