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
        if signature is None:
            cards = []
            for owner, key, _seq, raw in _canonical(_task_rows(conn, meta.get("gateway"))):
                with contextlib.suppress(TypeError, ValueError):
                    task = json.loads(raw)
                    if isinstance(task, dict) and task.get("status") in _TERMINAL:
                        cards.append((owner, key, task))
            return ready, cards, signature
        cards = _changed_rows_cards(conn, replica, meta.get("gateway"))
    return ready, cards, signature


# (replica, owner, key) -> (seq, terminal summary or None). The replica holds thousands of
# task rows (~30 MB of JSON on a busy fleet) and its signature moves whenever any card does,
# so decoding every row per change kept the notice pollers busy while Desktop sat idle. A row
# is re-read only when its seq moved; the summary keeps just the fields _persist reads.
_decoded: dict[tuple, tuple] = {}
_decoded_files: dict[str, tuple | None] = {}  # replica -> file identity its _decoded rows came from


def _summary(task) -> dict | None:
    if not isinstance(task, dict) or task.get("status") not in _terminal_states():
        return None
    run = task.get("run")
    run = run if isinstance(run, dict) else {}
    notice = task.get("completion_notice")
    notice = notice if isinstance(notice, dict) else {}
    return {"status": task.get("status"), "completed_at": task.get("completed_at"),
            "run": {"ended_at": run.get("ended_at")},
            "completion_notice": {"created_at": notice.get("created_at")}}


def _terminal_states():
    from tui_gateway.kanban_remote_notices import _TERMINAL
    return _TERMINAL


def _changed_rows_cards(conn, replica: Path, me) -> list[tuple]:
    from tui_gateway.kanban_remote_notices import _canonical

    sql = "SELECT gw, key, seq, NULL FROM replica WHERE coll = 'tasks'"
    rows = conn.execute(sql + " AND gw != ?", (me,)).fetchall() if me else conn.execute(sql).fetchall()
    rows = _canonical(rows)
    tag = str(replica)
    seen = set()
    cards = []
    for owner, key, seq, _ in rows:
        ident = (tag, owner, key)
        seen.add(ident)
        hit = _decoded.get(ident)
        if hit is None or hit[0] != seq:
            raw = conn.execute("SELECT data FROM replica WHERE gw = ? AND coll = 'tasks' AND key = ?",
                               (owner, key)).fetchone()
            summary = None
            with contextlib.suppress(TypeError, ValueError):
                summary = _summary(json.loads(raw[0])) if raw else None
            hit = _decoded[ident] = (seq, summary)
        if hit[1] is not None:
            cards.append((owner, key, hit[1]))
    for ident in [ident for ident in _decoded if ident[0] == tag and ident not in seen]:
        del _decoded[ident]
    return cards


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

    An unchanged replica change key (the observer's data_version) skips the replica read entirely; a key is kept
    only when it brackets the read. A commit racing the read keeps the decoded rows of the same db file (their
    seqs still gate every re-decode); only a replaced file or an unknown key forgets them.
    Cache timestamps, not freshness verdicts: a quiet replica must still age out.
    Ledger replacement invalidates the cache; acknowledgements need not invalidate it.
    """
    from tui_gateway.kanban_remote_notices import ledger_path
    from tui_gateway.kanban_replica_observer import file_identity, fingerprint

    ledger = Path(ledger) if ledger is not None else ledger_path()
    key = (str(replica.resolve()), str(ledger.resolve()))
    tag = str(replica)
    with _lock:
        stamp = fingerprint(replica)
        ident = file_identity(stamp)
        cached = _memo.get(key)  # (signature, fresh, ledger id, key or None, replica file identity)
        if cached is not None and (cached[2] != _file_id(ledger) or cached[4] != ident):
            _memo.pop(key)  # a replaced ledger, or a replaced/recreated/unreadable replica
            cached = None
        if cached is not None and stamp is not None and cached[3] == stamp:
            return cached[1]
        if ident is None or _decoded_files.pop(tag, None) != ident:
            _forget_decoded(tag)  # another db file may reuse a key at an equal seq with other contents
        ready, cards, signature = _snapshot(replica, cached[0] if cached else None)
        after = fingerprint(replica)
        if stamp is None or after != stamp:
            stamp = None  # a commit raced the read: keep nothing that would skip the next poll
            ident = ident if file_identity(after) == ident else None
        _decoded_files[tag] = ident
        while len(_decoded_files) > 16:
            _forget_decoded(next(iter(_decoded_files)))
        if not ready:
            return {}
        if cards is None and cached is not None:
            fresh = cached[1]
        else:
            fresh = _persist(key[0], cards or [], ledger, now)
        if ident is not None:
            _memo[key] = (signature, fresh, _file_id(ledger), stamp, ident)
        else:
            _memo.pop(key, None)
        while len(_memo) > 16:
            _memo.pop(next(iter(_memo)))
        return fresh


def _forget_decoded(tag: str) -> None:
    _decoded_files.pop(tag, None)
    for ident in [ident for ident in _decoded if ident[0] == tag]:
        del _decoded[ident]
