"""Every module in this package is imported on startup, so dropping a new file with an
@collector-decorated function here (plus an entry in config.yaml) is enough to add a source."""

import importlib
import pkgutil

from trade_news.collectors.base import REGISTRY, Batch, Context, RawItem, collector


def discover() -> dict:
    for mod in pkgutil.iter_modules(__path__):
        if not mod.name.startswith("_") and mod.name != "base":
            importlib.import_module(f"{__name__}.{mod.name}")
    return REGISTRY


__all__ = ["REGISTRY", "Batch", "Context", "RawItem", "collector", "discover"]
