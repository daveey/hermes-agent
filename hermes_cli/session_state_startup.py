"""SessionState v2 shadow-mode startup for ``hermes serve``. [daveey]

``session_state.enabled: false`` in config.yaml turns it off. It never creates a state.db: a home without one has no
sessions to track. Startup and shutdown run off the event loop; a failure to start is logged and leaves the GET/WS
routes answering 503.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

logger = logging.getLogger(__name__)
SEED_LIMIT = 200
SEED_WINDOW_S = 86400.0


def enabled() -> bool:
    """``session_state.enabled`` in config.yaml (default true): the shadow store's rollback switch."""
    try:
        from hermes_cli.config import cfg_get, load_config_readonly

        value = cfg_get(load_config_readonly(), "session_state", "enabled", default=True)
    except (ImportError, OSError, ValueError):
        return True
    return value not in (False, 0, "0", "off", "false", "no")


def start(home: Any = None, *, tick_s: float = 0.1) -> Optional[Any]:
    """Open the profile's state.db, install the process store and its watchers, seed recent sessions."""
    import time
    from pathlib import Path

    from hermes_constants import get_hermes_home
    from hermes_session_state_events import install_watchers
    from hermes_session_state_store import SessionStateStore, install
    from hermes_state import SessionDB

    home = Path(home) if home is not None else get_hermes_home()
    state_path = home / "state.db"
    if not state_path.is_file():
        return None
    db = SessionDB(db_path=state_path)
    store = SessionStateStore(db, home=home, divergence_path=home / "logs" / "session-state-divergence.jsonl")
    install_watchers(store, state_path=state_path, user_steps_path=home / "user-steps" / "actions.sqlite3")
    rows = db._read_all("SELECT id FROM sessions WHERE outcome_at > ? ORDER BY outcome_at DESC LIMIT ?",
                        (time.time() - SEED_WINDOW_S, SEED_LIMIT))
    for row in rows:
        store.emit_session(str(row["id"]), "seed")
    install(store)
    store.start(tick_s=tick_s)
    return store


def stop(store: Any) -> None:
    from hermes_session_state_store import current, install

    store.stop()
    if current() is store:
        install(None)
    store.db.close()


@asynccontextmanager
async def lifespan(app: Any):
    store = None
    if enabled():
        try:
            store = await asyncio.to_thread(start)
        except Exception:  # health: allow BLE001 -- shadow mode never blocks the server's startup
            logger.warning("session state store unavailable", exc_info=True)
    try:
        yield
    finally:
        if store is not None:
            await asyncio.to_thread(stop, store)
