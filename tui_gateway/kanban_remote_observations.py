"""Durable local arrival times, separate from notice claims and owner clocks."""
from __future__ import annotations

import contextlib
import json
import threading
from pathlib import Path
from hermes_cli.kanban_withdrawal_notice import envelope as _withdrawal
from tui_gateway.kanban_remote_notice_state import state as _notice_state

_lock = threading.Lock()
_memo: dict[tuple, tuple] = {}
_generation: dict[tuple, int] = {}  # observe key -> ledger writes started, so only the newest publishes its memo


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
                    if isinstance(task, dict) and (task.get("status") in _TERMINAL or _withdrawal(task) is not None):
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
    if not isinstance(task, dict):
        return None
    withdrawal = _withdrawal(task)
    if withdrawal is not None:
        return {"status": task["status"], "withdrawal_notice": withdrawal}
    if task.get("status") not in _terminal_states():
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

    # One SQLite result row avoids a GIL reacquisition per task while observe's
    # shared lock is held; payload point reads remain proportional to changes.
    sql = "SELECT json_group_array(json_array(gw, key, seq, NULL)) FROM replica WHERE coll = 'tasks'"
    row = conn.execute(sql + " AND gw != ?", (me,)).fetchone() if me else conn.execute(sql).fetchone()
    rows = _canonical([tuple(item) for item in json.loads(row[0])])
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
    """Seed retained history once; later current-key states receive one local window.

    [daveey] ``observations`` rows are insert-only, so a ``(source, ref, state)`` already read from (or written to)
    this ledger file keeps its ``first_seen`` for good: it is answered from ``_known`` and only cards in a state not
    seen yet touch SQLite. Re-reading every terminal card per replica change was most of the notice pollers' wait.
    ``observe`` calls this without ``_lock``; concurrent callers serialize on the ledger's ``BEGIN IMMEDIATE`` and,
    the rows being insert-only, agree on every ``first_seen``. Nothing enters ``_known`` before its commit.
    """
    from tui_gateway.kanban_remote_notices import FRESH_S, _fresh_at, _ledger

    ledger = Path(ledger)
    slot = (str(ledger), source)
    file_id = _file_id(ledger)
    with _known_lock:
        entry = _known.get(slot)
    known = entry[1] if entry is not None and file_id is not None and entry[0] == file_id else None
    states = []
    missing = []
    for owner, key, task in cards:
        at, state, ref = _fresh_at(task), _notice_state(task), f"{owner}/{key}"
        states.append((owner, key, state, ref, at))
        if known is None or (ref, state) not in known:
            missing.append((key, state, ref, at))
    if known is not None and not missing:  # an entry also proves the source's baseline is recorded
        return _fresh_map(states, known)
    stats["transactions"] += 1
    added = {}
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
            for key, state, ref, at in missing:
                # Unknown ages and frozen legacy keys cannot establish local freshness.
                seen = now if at is not None and key.count('/') >= 2 and (not initial or now - at <= FRESH_S) else None
                row = conn.execute("SELECT first_seen FROM observations WHERE source=? AND ref=? AND state=?",
                                   (source, ref, state)).fetchone()
                if row is None:
                    conn.execute("INSERT INTO observations VALUES (?,?,?,?)", (source, ref, state, seen))
                added[(ref, state)] = row[0] if row is not None else seen
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    after = _file_id(ledger)
    merged = {**(known or {}), **added} if after == file_id else added
    if after is not None:
        with _known_lock:  # a racing caller's entries may be dropped here: they are only re-read, never wrong
            _known[slot] = (after, merged)
            while len(_known) > 16:
                _known.pop(next(iter(_known)))
    return _fresh_map(states, {**(known or {}), **added})


def _fresh_map(states: list[tuple], first_seen: dict) -> dict[tuple, float | None]:
    fresh = {}
    for owner, key, state, ref, at in states:
        first = first_seen[(ref, state)]
        fresh[(owner, key, state)] = max(at, first) if at is not None and first is not None else at
    return fresh


_known_lock = threading.Lock()
# (ledger, source) -> (ledger file identity, {(ref, state): first_seen}): rows known to be in that ledger file.
_known: dict[tuple, tuple] = {}
stats = {"transactions": 0}


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
            _remember(key, (signature, fresh, _file_id(ledger), stamp, ident))
            return fresh
        _generation[key] = generation = _generation.get(key, 0) + 1
    # [daveey] The ledger write runs without ``_lock``: holding it there kept every notice poller waiting on one
    # poller's transaction. ``cards`` is this call's own snapshot, so the answer is the one it would have had under
    # the lock; only the newest snapshot started for ``key`` publishes its memo, and only after its commit.
    fresh = _persist(key[0], cards or [], ledger, now)
    with _lock:
        if _generation.get(key) == generation:
            _remember(key, (signature, fresh, _file_id(ledger), stamp, ident))
        return fresh


def _remember(key: tuple, entry: tuple) -> None:
    """Publish (caller holds ``_lock``). An entry without a replica file identity is never kept."""
    if entry[4] is not None:
        _memo[key] = entry
    else:
        _memo.pop(key, None)
    while len(_memo) > 16:
        _memo.pop(next(iter(_memo)))


def _forget_decoded(tag: str) -> None:
    _decoded_files.pop(tag, None)
    for ident in [ident for ident in _decoded if ident[0] == tag]:
        del _decoded[ident]
