"""One host copy of the remote notice index per replica change (design t_01a1d45d75851804 §2c). [daveey]

Every chat's notice poller (``kanban_remote_notices.collect``) needs the same thing from the hub replica: the
terminal remote cards linked to each ``(profile, session)``, plus this host's first observation of each state
(``kanban_remote_observations.observe``). Each backend process rebuilt both for every replica change (memoized
only per process): in a steady hermes-hub serve ``remote_index_projection.snapshot`` was 11.5% of the samples and
``observe`` 2.1%, the same answer computed once per process.

Here whoever builds that answer for a replica state publishes it to ``<ledger dir>/remote-index.db``, in one
transaction:

* ``meta``: ``key`` (the replica state the rows belong to, :func:`replica_key`), ``me`` (this gateway), ``ledger``
  (identity of the observation ledger the first-seen times came from), ``hub_db``, ``built_at``.
* ``cards``: ``(owner, key, seq, state, task, obs, fresh, first_seen)``, one per linked terminal remote card, with
  ``observe``'s answer for its state (``obs`` = the state is in that map).
* ``links``: ``(profile, session, owner, key, ord)``, the index entries in the order the local build lists them.

A reader (:func:`lookup`) whose current key equals the published one reads only its own ``(profile, session ∈
lineage)`` rows and skips ``observe`` (the first-seen times are in the rows; the publisher's ``observe`` already
recorded them in the ledger). Any other answer (no file, a stale key, a corrupt file, a busy file) is a miss: the
caller computes locally exactly as before and then offers its result (:func:`publish`). Nothing here ever waits:
reads use a zero busy timeout, and publishing takes a NON-blocking ``flock`` and is skipped when another process
is publishing or a reader keeps the file busy (only a corrupt file is recreated). Both run outside
``kanban_remote_notices._lock``.

A publish writes only the rows whose replica ``seq`` or observation changed (per-seq deltas), so its cost follows
the change, like the incremental in-process build it replaces.

The holder (host loop lease ``kanban-remote-index``, :func:`ensure_holder`) watches the replica's change key once
a second and republishes as soon as it moves, so the readers' 5 s polls normally hit. With the holder dead every
reader still computes on a miss, so a notice is never later than the poll that would have found it before.
Only the root home runs a holder; a profile home's ledger gets its copy from its own readers' misses.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

try:
    import fcntl
except ImportError:  # no flock: never publish, every reader computes locally (today's behaviour)
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

VERSION = 1
LEASE = "kanban-remote-index"
LEASE_INTERVAL_S = 5.0
REFRESH_POLL_S = 1.0
BACKOFF_MAX_S = 60.0  # longest wait between holder retries while its refreshes keep failing
_MEMO_MAX = 512
_clock = time.monotonic

_memo_lock = threading.Lock()  # guards the dicts below only; never held across I/O
_keys: dict[tuple, tuple] = {}  # (hub, ledger) -> (stamp, replica key or None)
_answers: dict[tuple, Optional[tuple]] = {}  # (hub, ledger, stamp, profile, sids) -> hit or None (miss)
_published: dict[tuple, tuple] = {}  # (hub, ledger) -> (key, ledger file id) this process published or found
_decoded: dict[tuple, tuple] = {}  # (replica base, owner, key) -> (seq, task): payloads this process parsed
_DECODED_MAX = 20_000
_holders: dict[tuple, tuple] = {}  # (hub, ledger) -> (stop event, role priority) of this process's refresher
stats = {"hits": 0, "misses": 0, "publishes": 0, "busy": 0, "rows_written": 0}


def index_path(ledger: Path) -> Path:
    return Path(ledger).parent / "remote-index.db"


def _file_id(path: Path) -> Optional[list]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_dev, st.st_ino]


def replica_key(hub: Path) -> Optional[str]:
    """The replica state the index is a function of, comparable across processes; None when unknown.

    The replica's file identity, its gateway, whether it is caught up (``observe`` answers nothing before), and
    ``COUNT/MAX/TOTAL(seq)`` of the other gateways' task rows (a covering index scan, no payload read). The hub
    client gives every task write a new, higher ``seq`` (the in-process projection reuses rows on the same
    assumption), so any task commit moves the key. A replica without ``seq`` has no key: read locally."""
    from tui_gateway.kanban_remote_notices import _ro

    ident = _file_id(hub)
    if ident is None:
        return None
    try:
        with contextlib.closing(_ro(hub)) as conn:
            conn.execute("BEGIN")
            if "seq" not in {row[1] for row in conn.execute("PRAGMA table_info(replica)")}:
                return None
            meta = dict(conn.execute("SELECT k, v FROM meta WHERE k IN ('gateway', 'cursor', 'head')"))
            me = str(meta.get("gateway") or "")
            ready = False
            with contextlib.suppress(TypeError, ValueError):
                ready = bool(me) and int(str(meta.get("cursor"))) >= int(str(meta.get("head")))
            sig = conn.execute("SELECT COUNT(*), MAX(seq), TOTAL(seq) FROM replica WHERE coll = 'tasks' AND gw != ?",
                               (me,)).fetchone()
    except (OSError, sqlite3.Error):
        return None
    if _file_id(hub) != ident:
        return None
    return json.dumps([VERSION, os.path.realpath(hub), *ident, me, ready, *sig])


def _slot(hub: Path, ledger: Path) -> tuple:
    return str(hub), str(ledger)


def _remember(store: dict, key, value, limit: int = _MEMO_MAX) -> None:
    """Caller holds ``_memo_lock``."""
    store.pop(key, None)
    store[key] = value
    while len(store) > limit:
        store.pop(next(iter(store)))


def current_key(hub: Path, ledger: Path, stamp) -> Optional[str]:
    """:func:`replica_key` memoized on the observer's change key (one query per replica change per process)."""
    if stamp is None:
        return None
    slot = _slot(hub, ledger)
    with _memo_lock:
        held = _keys.get(slot)
    if held is not None and held[0] == stamp:
        return held[1]
    key = replica_key(hub)
    with _memo_lock:
        _remember(_keys, slot, (stamp, key))
        for old in [k for k in _answers if k[:2] == slot and k[2] != stamp]:
            del _answers[old]
    return key


def lookup(hub: Path, ledger: Path, stamp, profile: str, sids: tuple) -> Optional[tuple]:
    """``(me, {sid: [card, ...]}, observed)`` from the published copy, or None (compute locally).

    Answers (hit or miss) are memoized per change key, so an unchanged replica opens nothing here."""
    from tui_gateway.kanban_remote_observations import Observed

    key = current_key(hub, ledger, stamp)
    if key is None:
        return None
    memo_key = (*_slot(hub, ledger), stamp, profile, sids)
    with _memo_lock:
        if memo_key in _answers:
            return _answers[memo_key]
    rows = _read(index_path(ledger), key, ledger, profile, sids)
    answer = None
    if rows is not None:
        me, found = rows
        by_sid: dict[str, list] = {sid: [] for sid in sids}
        observed = Observed()
        for sid, owner, card_key, state, task, obs, fresh, first_seen in found:
            by_sid[sid].append({"gw": owner, "key": card_key, "task": task})
            if obs:
                observed[(owner, card_key, state)] = fresh
                if first_seen is not None:
                    observed.first_seen[(owner, card_key, state)] = first_seen
        answer = (me, by_sid, observed)
    with _memo_lock:
        stats["hits" if answer is not None else "misses"] += 1
        _remember(_answers, memo_key, answer)
    return answer


def _read(db: Path, key: str, ledger: Path, profile: str, sids: tuple) -> Optional[tuple]:
    """This chat's rows when ``meta.key`` is ``key`` and the ledger is the one they were observed in. Never waits.

    A card whose ``seq`` this process already decoded (same replica file) reuses that task: only payloads that
    changed are fetched and parsed, so a long-lived chat with hundreds of linked cards costs one changed card per
    replica change, not all of them."""
    if not sids or not db.is_file():
        return None
    base = tuple(json.loads(key)[:4])
    try:
        with contextlib.closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.0)) as conn:
            conn.execute("BEGIN")
            meta = dict(conn.execute("SELECT k, v FROM meta WHERE k IN ('key', 'me', 'ledger')"))
            if meta.get("key") != key or meta.get("ledger") != json.dumps([str(ledger), _file_id(ledger)]):
                return None
            marks = ",".join("?" * len(sids))
            rows = conn.execute(
                "SELECT l.session, c.owner, c.key, c.state, c.seq, c.obs, c.fresh, c.first_seen FROM links l "
                f"JOIN cards c ON c.owner = l.owner AND c.key = l.key WHERE l.profile = ? AND l.session IN ({marks}) "
                "ORDER BY l.ord", (profile, *sids)).fetchall()
            found = []
            for sid, owner, card_key, state, seq, *obs in rows:
                ident = (base, owner, card_key)
                with _memo_lock:
                    hit = _decoded.get(ident)
                if hit is None or not isinstance(seq, int) or seq <= 0 or hit[0] != seq:
                    raw = conn.execute("SELECT task FROM cards WHERE owner = ? AND key = ?", (owner, card_key))
                    hit = (seq, json.loads(raw.fetchone()[0]))
                    with _memo_lock:
                        _remember(_decoded, ident, hit, limit=_DECODED_MAX)
                found.append((sid, owner, card_key, state, hit[1], *obs))
            return str(meta.get("me") or ""), found
    except (sqlite3.Error, ValueError, TypeError):  # corrupt, busy, half-created: compute locally
        logger.debug("remote notice index %s unreadable; computing locally", db, exc_info=True)
        return None


def _build_lock(db: Path) -> Optional[int]:
    """Non-blocking exclusive lock for publishing ``db``: an fd, or None when another process publishes now."""
    if fcntl is None:
        return None
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(f"{db}.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)",
    "CREATE TABLE IF NOT EXISTS cards (owner TEXT NOT NULL, key TEXT NOT NULL, seq INTEGER, state TEXT NOT NULL, "
    "task TEXT NOT NULL, obs INTEGER NOT NULL, fresh REAL, first_seen REAL, PRIMARY KEY (owner, key))",
    "CREATE TABLE IF NOT EXISTS links (profile TEXT NOT NULL, session TEXT NOT NULL, owner TEXT NOT NULL, "
    "key TEXT NOT NULL, ord INTEGER NOT NULL, PRIMARY KEY (profile, session, owner, key))",
)


_CORRUPT_CODES = (11, 26)  # SQLITE_CORRUPT, SQLITE_NOTADB (primary result codes)
_BUSY_CODES = (5, 6)  # SQLITE_BUSY, SQLITE_LOCKED


def _primary(exc: sqlite3.Error) -> Optional[int]:
    code = getattr(exc, "sqlite_errorcode", None)
    return code & 0xFF if isinstance(code, int) else None


def _busy(exc: sqlite3.Error) -> bool:
    code = _primary(exc)
    if code is not None:
        return code in _BUSY_CODES
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _corrupt(exc: sqlite3.Error) -> bool:
    """True only for a damaged file. 'database is locked' / busy (a reader's transaction blocking the switch to
    WAL, say) is an ``OperationalError`` too, and recreating then would delete a healthy copy."""
    code = _primary(exc)
    if code is not None:
        return code in _CORRUPT_CODES
    text = str(exc).lower()
    return "file is not a database" in text or "malformed" in text


def _open_rw(db: Path) -> sqlite3.Connection:
    """The index, created or (corrupt) recreated. Only the build-lock holder calls this, so nothing waits.

    A busy or locked file raises (the publish is skipped and readers compute locally); only real corruption
    unlinks and recreates it."""
    for attempt in (0, 1):
        conn = sqlite3.connect(str(db), timeout=0.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            for sql in _SCHEMA:
                conn.execute(sql)
            conn.execute("SELECT COUNT(*) FROM cards").fetchone()
            return conn
        except sqlite3.DatabaseError as exc:
            conn.close()
            if attempt or not _corrupt(exc):
                raise
            logger.warning("remote notice index %s unreadable; recreating it", db, exc_info=True)
            for suffix in ("", "-wal", "-shm"):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(f"{db}{suffix}")
    raise AssertionError("unreachable")


def publish(hub: Path, ledger: Path, key: str, me: str, index: dict, observed: dict, seqs: dict) -> bool:
    """Write ``index`` (``{(profile, session): [card, ...]}``) and ``observed`` for ``key``; True when the copy now
    holds ``key``. Skipped (False) while another process publishes. Only changed rows are written."""
    from tui_gateway.kanban_remote_notice_state import state as notice_state
    from tui_gateway.kanban_remote_observations import first_seen_of

    slot = _slot(hub, ledger)
    if _already(hub, ledger, key):
        return True
    db = index_path(ledger)
    fd = _build_lock(db)
    if fd is None:
        with _memo_lock:
            stats["busy"] += 1
        return False
    try:
        with contextlib.closing(_open_rw(db)) as conn:
            meta = dict(conn.execute("SELECT k, v FROM meta"))
            ledger_id = json.dumps([str(ledger), _file_id(ledger)])
            if meta.get("key") == key and meta.get("ledger") == ledger_id:
                written = 0
            else:
                cards, links = {}, set()
                for (profile, session), entries in index.items():
                    for ord_, card in enumerate(entries):
                        owner, card_key, task = card["gw"], card["key"], card["task"]
                        state = notice_state(task)
                        ident = (owner, card_key, state)
                        obs = (int(ident in observed), observed.get(ident), first_seen_of(observed, ident))
                        cards[(owner, card_key)] = (seqs.get((owner, card_key)), state, task, obs)
                        links.add((profile, session, owner, card_key, ord_))
                written = _write(conn, meta, key, me, ledger_id, str(hub), cards, links)
        with _memo_lock:
            _remember(_published, slot, (key, _file_id(ledger)))
            stats["publishes"] += 1
            stats["rows_written"] += written
        return True
    except (OSError, sqlite3.Error) as exc:
        if isinstance(exc, sqlite3.Error) and _busy(exc):
            with _memo_lock:
                stats["busy"] += 1
        logger.debug("remote notice index %s not published", db, exc_info=True)
        return False
    finally:
        os.close(fd)  # closing the descriptor drops the flock


def _write(conn, meta: dict, key: str, me: str, ledger_id: str, hub: str, cards: dict, links: set) -> int:
    """One transaction: the delta against the copy on disk (all of it when the copy is of another replica file)."""
    base = json.loads(key)[:4]
    same_file = meta.get("base") == json.dumps(base)
    old_cards = {(row[0], row[1]): (row[2], row[3], (row[4], row[5], row[6])) for row in conn.execute(
        "SELECT owner, key, seq, state, obs, fresh, first_seen FROM cards")} if same_file else {}
    old_links = set(conn.execute("SELECT profile, session, owner, key, ord FROM links")) if same_file else set()
    upserts = []
    for ident, (seq, state, task, obs) in cards.items():
        old = old_cards.get(ident)
        if old is not None and isinstance(seq, int) and seq > 0 and old[0] == seq and old[1] == state \
                and old[2] == obs:
            continue
        upserts.append((*ident, seq, state, json.dumps(task), *obs))
    gone = [ident for ident in old_cards if ident not in cards]
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if not same_file:
            conn.execute("DELETE FROM cards")
            conn.execute("DELETE FROM links")
        conn.executemany("DELETE FROM cards WHERE owner = ? AND key = ?", gone)
        conn.executemany("INSERT OR REPLACE INTO cards VALUES (?,?,?,?,?,?,?,?)", upserts)
        conn.executemany("DELETE FROM links WHERE profile = ? AND session = ? AND owner = ? AND key = ? AND ord = ?",
                         list(old_links - links))
        conn.executemany("INSERT OR REPLACE INTO links VALUES (?,?,?,?,?)", list(links - old_links))
        conn.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [
            ("key", key), ("me", me), ("ledger", ledger_id), ("hub_db", hub), ("base", json.dumps(base)),
            ("built_at", str(time.time())), ("version", str(VERSION))])
    return len(upserts) + len(gone)


def compute(hub: Path, ledger, ledger_file: Path, now: float, key: Optional[str]) -> tuple:
    """The local build (exactly what ``collect`` did before), then an offer to publish it. One
    ``remote_index.compute`` pass (loop_accounting).

    ``key`` was read before the build; the copy is published only if the replica still has it after ``observe``,
    so rows and first-seen times belong to that one state. ``kanban_remote_notices._lock`` is held only for the
    index build; publishing runs after it is released."""
    from hermes_cli import loop_accounting
    with loop_accounting.pass_("remote_index.compute"):
        return _compute(hub, ledger, ledger_file, now, key)


def _compute(hub: Path, ledger, ledger_file: Path, now: float, key: Optional[str]) -> tuple:
    from tui_gateway import kanban_remote_notices as krn
    from tui_gateway import remote_index_projection
    from tui_gateway.kanban_remote_observations import observe

    with krn._lock:
        me, index = krn._index(hub)
        seqs = remote_index_projection.seqs(index)
    observed = observe(hub, ledger, now)
    if key is not None and not _already(hub, ledger_file, key) and replica_key(hub) == key:
        publish(hub, ledger_file, key, me, index, observed, seqs)
    return me, index, observed


def _already(hub: Path, ledger: Path, key: str) -> bool:
    """This process already published (or found) ``key`` for this ledger file."""
    with _memo_lock:
        return _published.get(_slot(hub, ledger)) == (key, _file_id(ledger))


def published_key(ledger: Path) -> Optional[str]:
    """The key of the copy on disk (None when absent or unreadable)."""
    db = index_path(ledger)
    if not db.is_file():
        return None
    try:
        with contextlib.closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.0)) as conn:
            meta = dict(conn.execute("SELECT k, v FROM meta WHERE k IN ('key', 'ledger')"))
    except sqlite3.Error:
        return None
    if meta.get("ledger") != json.dumps([str(ledger), _file_id(ledger)]):
        return None
    return meta.get("key")


def refresh(hub: Path, ledger: Path, now: Optional[float] = None) -> bool:
    """Holder pass: republish when the copy is not of the replica's current state. True when it is current.
    One ``remote_index.refresh`` pass (loop_accounting): a current copy is a skip, a rebuild a key change."""
    from hermes_cli import loop_accounting
    with loop_accounting.pass_("remote_index.refresh") as acct:
        key = replica_key(hub)
        if key is None:
            acct.skip()
            return False
        if published_key(ledger) == key:
            acct.skip()
            return True
        acct.change()
        compute(hub, ledger, ledger, time.time() if now is None else now, key)
        return published_key(ledger) == key


def _hold(lease, hub: Path, ledger: Path, stop: threading.Event, poll: float = REFRESH_POLL_S) -> None:
    """Change-driven refresher: one ``PRAGMA data_version`` per ``poll`` while held, a rebuild per replica change.

    A refresh that does not leave the copy current (publishing fails, the replica has no key) is retried after a
    backoff that doubles up to :data:`BACKOFF_MAX_S`, not on every poll: readers compute locally on a miss
    meanwhile. A success resets it and remembers the stamp, so that state is not rebuilt. The backoff belongs to
    one holding and one replica state: re-acquiring the lease or a new stamp starts it over (an immediate
    rebuild), so a failing streak never delays the next state's copy by up to :data:`BACKOFF_MAX_S`."""
    from tui_gateway.kanban_replica_observer import fingerprint

    last, tried, holding, retry_at, delay = None, None, False, 0.0, poll
    try:
        while not stop.wait(poll):
            if not lease.held():
                last, holding = None, False
                continue
            if not holding:  # (re-)acquired: the last holding's failing streak says nothing about this one
                holding, retry_at, delay = True, 0.0, poll
            try:
                stamp = fingerprint(hub) if hub.is_file() else None
                if stamp is not None and stamp != tried:  # a replica state not tried yet: no inherited wait
                    retry_at, delay = 0.0, poll
                if stamp is not None and stamp != last and _clock() >= retry_at:
                    tried = stamp
                    if refresh(hub, ledger):
                        last, retry_at, delay = stamp, 0.0, poll
                    else:
                        retry_at, delay = _clock() + delay, min(delay * 2, BACKOFF_MAX_S)
            except Exception:  # health: allow BLE001 -- the readers compute on a miss; the holder must not die
                logger.debug("remote notice index refresh failed", exc_info=True)
                retry_at, delay = _clock() + delay, min(delay * 2, BACKOFF_MAX_S)
            lease.beat()
    finally:
        lease.release()


def ensure_holder(role: str) -> None:
    """Start this process's refresher for the root home's ledger (it runs only while it holds the lease).

    One refresher per process and ledger, under the highest role any caller claimed: a gateway's claim after a
    'serve' one (``collect`` ran first in the gateway process) stops the 'serve' refresher and starts a 'gateway'
    one, so the gateway outranks Desktop backends however its calls are ordered. Without ``fcntl`` nothing
    starts: publishing needs ``flock``, so a holder could never publish and would rebuild every poll.

    Not under pytest: a refresher left running by one test would rebuild that test's replica through the shared
    in-process memos while later tests run. Tests drive :func:`refresh` and :func:`_hold` directly."""
    from hermes_cli.host_loop_lease import ROLE_PRIORITY
    from hermes_constants import get_default_hermes_root, get_hermes_home
    from tui_gateway.kanban_remote_notices import hub_db_path, ledger_path

    if os.environ.get("PYTEST_CURRENT_TEST") or fcntl is None:
        return
    try:
        if os.path.realpath(get_hermes_home()) != os.path.realpath(get_default_hermes_root()):
            return
        hub, ledger = hub_db_path(), ledger_path()
    except (OSError, ValueError):
        return
    slot, rank = _slot(hub, ledger), ROLE_PRIORITY.get(role, 0)
    with _memo_lock:
        held = _holders.get(slot)
        if held is not None and held[1] >= rank:
            return
        if held is not None:
            held[0].set()  # the lower-role refresher stops after its current pass and releases its lease
        stop = threading.Event()
        _holders[slot] = (stop, rank)
    from agent.memory_provider import spawn_context_thread
    from hermes_cli.host_loop_lease import HostLoopLease

    lease = HostLoopLease(LEASE, role, interval=LEASE_INTERVAL_S)
    spawn_context_thread(_hold, name="kanban-remote-index", args=(lease, hub, ledger, stop)).start()


def reset() -> None:
    """Forget this process's memos and stop its refreshers (tests, a backend forgetting its memos)."""
    with _memo_lock:
        _keys.clear()
        _answers.clear()
        _published.clear()
        _decoded.clear()
        for name in stats:
            stats[name] = 0
        for stop, _rank in _holders.values():
            stop.set()
        _holders.clear()
