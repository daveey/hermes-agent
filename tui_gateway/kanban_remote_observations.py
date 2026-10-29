"""Durable local arrival times, separate from notice claims and owner clocks."""
from __future__ import annotations

import contextlib
import json
import threading
from pathlib import Path

_lock = threading.Lock()
_memo: dict[tuple, tuple] = {}


def _snapshot(replica: Path, previous=None) -> tuple[bool, list[tuple] | None, tuple | None]:
    """Read readiness and ALL canonical terminal states from the same SQLite snapshot.

    The hub client sets gateway before pulling and commits each feed page. Until
    cursor reaches head, an empty/partial replica must not establish a baseline.
    Include unlinked cards and historical done cards without completion evidence:
    linking a chat or switching between sweep and collect must not resurrect them.
    """
    from tui_gateway.kanban_remote_notices import _canonical, _ro, _task_rows, _TERMINAL

    with contextlib.closing(_ro(replica)) as conn:
        conn.execute("BEGIN")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
            return False, [], None
        meta = dict(conn.execute("SELECT k,v FROM meta WHERE k IN ('gateway','cursor','head')"))
        ready = False
        with contextlib.suppress(TypeError, ValueError):
            ready = bool(meta.get("gateway")) and int(str(meta.get("cursor"))) >= int(str(meta.get("head")))
        if not ready:
            return False, [], None
        cols = {row[1] for row in conn.execute("PRAGMA table_info(replica)")}
        signature = None
        if "seq" in cols:
            signature = (*sorted(meta.items()), *conn.execute(
                "SELECT COUNT(*), MAX(seq), TOTAL(seq) FROM replica WHERE coll='tasks'").fetchone())
        if signature is not None and signature == previous:
            return True, None, signature
        cards = []
        for owner, key, _seq, raw in _canonical(_task_rows(conn, meta.get("gateway"))):
            with contextlib.suppress(TypeError, ValueError):
                task = json.loads(raw)
                if isinstance(task, dict) and task.get("status") in _TERMINAL:
                    cards.append((owner, key, task))
    return ready, cards, signature


def _persist(source: str, cards: list[tuple], ledger, now: float) -> dict[tuple, float | None]:
    """Seed retained history once; later current-key states receive one local window."""
    from tui_gateway.kanban_remote_notices import FRESH_S, _fresh_at, _ledger, _terminal_at

    with contextlib.closing(_ledger(ledger)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS observation_sources (source TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE IF NOT EXISTS observations (source TEXT, ref TEXT, state TEXT, "
                     "first_seen REAL, PRIMARY KEY(source, ref, state))")
        # Serialize baseline + snapshot: concurrent backends must agree which is first.
        conn.execute("BEGIN IMMEDIATE")
        try:
            initial = conn.execute("SELECT 1 FROM observation_sources WHERE source=?", (source,)).fetchone() is None
            if initial:
                conn.execute("INSERT INTO observation_sources VALUES (?)", (source,))
            fresh = {}
            for owner, key, task in cards:
                at = _fresh_at(task)
                terminal = _terminal_at(task)
                state = f"{task['status']}@{int(terminal) if terminal else 0}"
                ref = f"{owner}/{key}"
                # Unknown ages and frozen legacy keys cannot establish local freshness.
                seen = now if at is not None and key.count('/') >= 2 and (not initial or now - at <= FRESH_S) else None
                row = conn.execute("SELECT first_seen FROM observations WHERE source=? AND ref=? AND state=?",
                                   (source, ref, state)).fetchone()
                if row is None:
                    conn.execute("INSERT INTO observations VALUES (?,?,?,?)", (source, ref, state, seen))
                first = row[0] if row is not None else seen
                fresh[(owner, key, state)] = max(at, first) if at is not None and first is not None else at
            conn.commit()
            return fresh
        except BaseException:
            conn.rollback()
            raise


def _file_id(path: Path) -> tuple | None:
    try:
        stat = path.stat()
        return stat.st_dev, stat.st_ino
    except FileNotFoundError:
        return None


def observe(replica: Path, ledger, now: float) -> dict[tuple, float | None]:
    """Only changed replica snapshots decode cards and touch the observation ledger.

    Cache timestamps, not freshness verdicts: a quiet replica must still age out.
    Ledger replacement invalidates the cache; acknowledgements need not invalidate it.
    """
    from tui_gateway.kanban_remote_notices import ledger_path

    ledger = Path(ledger) if ledger is not None else ledger_path()
    key = (str(replica.resolve()), str(ledger.resolve()))
    with _lock:
        cached = _memo.get(key)
        if cached is not None and cached[2] != _file_id(ledger):
            cached = None
        ready, cards, signature = _snapshot(replica, cached[0] if cached else None)
        if not ready:
            return {}
        if cards is None and cached is not None:
            return cached[1]
        fresh = _persist(key[0], cards or [], ledger, now)
        _memo[key] = (signature, fresh, _file_id(ledger))
        while len(_memo) > 16:
            _memo.pop(next(iter(_memo)))
        return fresh

