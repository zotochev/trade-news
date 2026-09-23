"""Collector contract and registry.

A collector is a plain function registered with @collector:

    @collector("my_source", secrets=("MY_API_KEY",))
    def fetch(ctx: Context, cursor: dict | None) -> Batch:
        resp = ctx.get("https://api.example.com/news", params={"since": ...})
        return Batch(items=[RawItem(...)], cursor={...})

It never touches the database: it receives the cursor it returned last time and gives back
items + the new cursor. The pipeline stores both in one transaction.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import httpx


@dataclass(frozen=True, slots=True)
class RawItem:
    source_item_id: str
    title: str | None
    body: str | None
    url: str | None
    published_at: datetime | None  # tz-aware
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Batch:
    items: list[RawItem]
    cursor: dict[str, Any] | None = None


class Getter(Protocol):
    def __call__(self, url: str, **kwargs: Any) -> httpx.Response: ...


@dataclass(frozen=True, slots=True)
class Context:
    source: str
    get: Getter  # rate-limited, retried HTTP GET
    params: Mapping[str, Any]  # `params` from config.yaml
    secrets: Mapping[str, str]  # only the env vars the collector declared
    now: Callable[[], datetime]
    log: Any


FetchFn = Callable[[Context, dict[str, Any] | None], Batch]


@dataclass(frozen=True, slots=True)
class CollectorSpec:
    name: str
    fetch: FetchFn
    secrets: tuple[str, ...] = ()
    # Title-based dedup makes sense for news. Primary data (e.g. filings) has templated titles
    # and unique ids, so it is deduplicated by URL/id only.
    title_dedup: bool = True


REGISTRY: dict[str, CollectorSpec] = {}


def collector(
    name: str,
    *,
    secrets: tuple[str, ...] = (),
    title_dedup: bool = True,
) -> Callable[[FetchFn], FetchFn]:
    def register(fn: FetchFn) -> FetchFn:
        if name in REGISTRY:
            raise ValueError(f"collector {name!r} registered twice")
        REGISTRY[name] = CollectorSpec(name, fn, secrets, title_dedup)
        return fn

    return register
