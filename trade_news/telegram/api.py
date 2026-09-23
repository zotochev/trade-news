"""Minimal Telegram Bot API caller: api("sendMessage", chat_id=..., text=...) -> result.

Docs: https://core.telegram.org/bots/api
Errors come back as {"ok": false, "error_code": N, "description": ..., "parameters": {...}};
429 carries parameters.retry_after. The token is part of the URL, so it is never logged.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_random_exponential

log = structlog.get_logger()

Api = Callable[..., Any]


class TelegramError(Exception):
    def __init__(self, method: str, error_code: int, description: str, retry_after: int | None):
        super().__init__(f"{method}: {error_code} {description}")
        self.error_code = error_code
        self.description = description
        self.retry_after = retry_after

    @property
    def chat_unreachable(self) -> bool:
        """Bot blocked by the user, kicked from the group, or the chat is gone."""
        return self.error_code == 403 or (
            self.error_code == 400 and "chat not found" in self.description.lower()
        )


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, TelegramError):
        return exc.error_code == 429 or exc.error_code >= 500
    return isinstance(exc, httpx.TransportError)


def make_api(client: httpx.Client, token: str, *, max_attempts: int = 4) -> Api:
    base = f"https://api.telegram.org/bot{token}"

    @retry(
        retry=retry_if_exception(_retryable),
        wait=wait_random_exponential(multiplier=1, max=30),
        stop=stop_after_attempt(max_attempts),
        reraise=True,
    )
    def api(method: str, *, http_timeout: float = 30, **params: Any) -> Any:
        try:
            resp = client.post(f"{base}/{method}", json=params, timeout=http_timeout)
        except httpx.TransportError as exc:
            # httpx messages may include the URL (and so the token): re-raise without it
            raise httpx.TransportError(f"{method}: {type(exc).__name__}") from None
        try:
            data = resp.json()
        except ValueError:
            data = {"ok": False, "error_code": resp.status_code, "description": resp.text[:200]}
        if data.get("ok"):
            return data.get("result")
        err = TelegramError(
            method,
            int(data.get("error_code") or resp.status_code),
            str(data.get("description", "")),
            (data.get("parameters") or {}).get("retry_after"),
        )
        if err.retry_after:
            log.warning("telegram_rate_limited", method=method, retry_after=err.retry_after)
            time.sleep(min(err.retry_after, 60))
        raise err

    return api
