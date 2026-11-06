"""Feature modules. Each `duo_monitor/features/<name>.py` may:
  - define `router = fastapi.APIRouter()` (auto-mounted on the app),
  - register hooks via `duo_monitor.hooks` (@on, @footer, @email_header),
  - create its own tables with `CREATE TABLE IF NOT EXISTS` in a `setup(conn)` function (called once at load).
Core files (web.py, poller.py, db.py, notify.py, messages.py) should not need edits to add a feature.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil

log = logging.getLogger("duo_monitor.features")
_loaded: list = []


def load(conn) -> list:
    """Import every feature module once, run each `setup(conn)` (idempotent: called again for every new
    connection/DB, e.g. per test) and return their routers."""
    if not _loaded:
        for m in pkgutil.iter_modules(__path__):
            if m.name.startswith("_"):
                continue
            try:
                _loaded.append(importlib.import_module(f"{__name__}.{m.name}"))
            except Exception:  # noqa: BLE001 — one broken feature must not take the site down
                log.exception("feature %s failed to import", m.name)
    for mod in _loaded:
        if hasattr(mod, "setup"):
            try:
                mod.setup(conn)
            except Exception:  # noqa: BLE001
                log.exception("feature %s setup failed", mod.__name__)
    return [mod.router for mod in _loaded if hasattr(mod, "router")]
