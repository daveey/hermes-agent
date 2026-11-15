"""J complete serve/gateway rows: change_watcher + session notification pollers on a moving replica. [daveey]

Card J (t_01a1d862f6e2fc9e). P1 run11 measured the rows as watcher + notification pollers in one process:
serve <= 0.7% of a core, gateway <= 0.8%. This replays 600 s of measured input against the real code, with the
virtual clock only at the blocking waits (``tests/tui_gateway/j_nonrpc_scheduler.py``):

- watcher: every production watch, real frames to ten transports, the chg1 state stream
  (140 commits: 83 message, 2 title, 55 activity), and the hub's three skill roots (exact counts);
- pollers: the real ``_notification_poller_scoped_loop`` with PollerStop deadlines, SQL, claims and custody.
  The poller count is P1's: 202 serve poll_kanban runs in 547 s (1.85 pollers) and 871 gateway runs in 3721 s
  (1.17), both rounded up to 2. Only ``_notif_run_submit`` (the model turn) is replaced, by a recorder;
- local board: ``tasks`` cards (1k / 5.8k / 20k) plus 12 new subscribed cards completed during the window;
- replica: ``replicas`` task rows (10k / 40k, real hub schema and indexes, ~4.7 KB payloads like the live
  replica) with the rf2 task stream doubled to 600 s: 80 change windows, 568 row changes, 12 of them new done
  cards for the two chats;
- holder: the lease ``kanban-remote-index`` beats and ``kanban_remote_index.refresh`` republishes on the first
  whole second at least 1 s after a replica commit (P1: 1 s poll + build). Its CPU is recorded, not charged to
  the row: it is its own host loop (``remote_index.refresh``), held by one process per host, and design §3
  books the remote index, like ``links_refresh`` (its own refresher thread), to H3, not J.

This is a count-preserving reconstruction: rf2/chg1 record counts per window, not the original order.
Sessions (5k / 23k) only enter the watcher and lineage reads; tasks only the local board polls; replica rows
only the remote collect. Each cell varies one dimension from the base, plus all-max with each WAL state, so
the <= 1.5x growth checks compare like with like. A >= 400 MiB WAL is held by a long reader on all three
databases (the only way a live WAL stays that large). Every cell runs once: no best-of, no retry.
The gateway row (<= 0.8%) runs the same watcher and pollers (P1: notifications.* spans in the gateway, 1.17
pollers), so the stricter serve bound is asserted for both.

These cells are minutes long, so they run only with ``J_ROWS_RUN=1`` (an operator benchmark, like the P1
profile it reproduces): CPU shares need a host that is not running sixteen other test files, and the
default suite's per-file cap is 300 s. The per-gate RED/GREEN tests stay in the default suite.
"""
import builtins
import contextlib
import io
import json
import os
import shutil
import sqlite3
import threading
import time
from collections import Counter
from types import SimpleNamespace

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_state import SessionDB
from tui_gateway import server, watch_store

CHATS = ("20261114_090000_a1b2c3", "20261114_090000_d4e5f6")
# Operator benchmark: J_ROWS_RUN=1 J_ROWS_EVIDENCE=<abs .jsonl> python -m pytest <this file> -p no:randomly
pytestmark = pytest.mark.skipif(not os.environ.get("J_ROWS_RUN"),
                                reason="J full-row benchmark: minutes per cell, set J_ROWS_RUN=1 on a quiet host")
SECONDS, TICK, POLLERS, WARM_TICKS = 600, 0.5, 2, 60
WAL_BYTES = 400 * 1024 * 1024
BASE = {"tasks": 1000, "replicas": 10_000, "sessions": 5000, "large_wal": False}
CELLS = {
    "base": BASE,
    "tasks5800": dict(BASE, tasks=5800),
    "tasks20k": dict(BASE, tasks=20_000),
    "replicas40k": dict(BASE, replicas=40_000),
    "sessions23k": dict(BASE, sessions=23_000),
    "max": {"tasks": 20_000, "replicas": 40_000, "sessions": 23_000, "large_wal": False},
    "max_wal400m": {"tasks": 20_000, "replicas": 40_000, "sessions": 23_000, "large_wal": True},
    "base_wal400m": dict(BASE, large_wal=True),
}
_RESULTS: dict = {}
# Every function tests/home_io_guard.py wraps (it keeps the original in __wrapped__).
_GUARDED = ((builtins, "open"), (io, "open"), (os, "mkdir"), (os, "unlink"), (os, "remove"), (os, "rmdir"),
            (os, "chmod"), (os, "utime"), (os, "stat"), (os, "lstat"), (os, "readlink"), (os, "access"),
            (os, "makedirs"), (os, "listdir"), (os, "scandir"), (os, "rename"), (os, "replace"),
            (shutil, "rmtree"), (sqlite3, "connect"), (os, "open"), (os, "close"))


@contextlib.contextmanager
def _production_io():
    """Measure production cost, not the suite's HomeIOGuard: its Python checks on every stat/open/connect
    doubled the measured row (0.55-0.62% vs 0.30% of a core for the base cell). Setup and the warm-up run
    guarded, and they exercise the same code paths against the same temporary home; only the measured
    window runs on the originals (the precedent is tests/gateway/test_status_pid_memo.py)."""
    saved = [(module, name, getattr(module, name)) for module, name in _GUARDED]
    for module, name, fn in saved:
        setattr(module, name, getattr(fn, "__wrapped__", fn))
    try:
        yield
    finally:
        for module, name, fn in saved:
            setattr(module, name, fn)


class RecordingTransport:
    def __init__(self):
        self.frames = []

    def write(self, frame):
        self.frames.append(frame)


def _pad_wal(path, holders):
    """Real committed WAL frames (not a sparse file) past 400 MiB, with checkpoints off. The padding
    connection stays open for the cell (``holders``), holding a read transaction: SQLite checkpoints and
    deletes the WAL when the last connection closes, and resets it once no reader needs its frames."""
    conn = sqlite3.connect(path)
    holders.append(conn)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE IF NOT EXISTS replay_padding (payload BLOB)")
    conn.commit()
    for _ in range(101):
        conn.execute("INSERT INTO replay_padding VALUES (zeroblob(4194304))")
        conn.commit()
    # A live WAL stays this large only while a long reader starves the checkpointer; without one the
    # production connections' autocheckpoint plus journal_size_limit cut it to 8-64 MiB within seconds.
    conn.execute("BEGIN")
    conn.execute("SELECT count(*) FROM replay_padding").fetchone()
    assert os.path.getsize(f"{path}-wal") >= WAL_BYTES


def _checkpoint(path):
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _filler_task(n):
    return {"id": f"t_fill{n:06d}", "status": "done", "title": f"filler {n}", "board": "default",
            "assignee": "default", "created_at": 1_700_000_000, "completed_at": 1_700_000_000 + n,
            "body": "x" * 4400, "comments": [], "last_comments": [], "parents": [], "children": [],
            "run": {"ended_at": 1_700_000_000 + n, "summary": "filler"},
            "sessions": [{"gw": "elsewhere", "profile": "default", "session": f"other_{n % 700}",
                          "rel": "shepherd"}]}


def _done_card(tid, chat, at):
    task = {"id": tid, "status": "done", "title": tid, "board": "default", "assignee": "default",
            "completed_at": at, "sessions": [{"gw": "macbook", "profile": "default", "session": chat,
                                              "rel": "shepherd"}],
            "completion_notice": {"event_id": 9000 + int(tid[-3:]) + (500 if "warm" in tid else 0),
                                  "payload": {"summary": f"done {tid}"}}}
    return f"default/default/{tid}", task


def _build_replica(path, rows):
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO meta VALUES (?, ?)",
                         [("gateway", "macbook"), ("cursor", "100"), ("head", "100")])
        conn.execute("CREATE TABLE replica (gw TEXT, coll TEXT, key TEXT, version INTEGER, seq INTEGER, "
                     "data TEXT, updated_at INTEGER, PRIMARY KEY (gw, coll, key))")
        conn.execute("CREATE INDEX replica_coll ON replica(coll)")
        conn.execute("CREATE INDEX replica_poll_metadata ON replica(coll, gw, seq)")
        conn.execute("CREATE INDEX replica_owner_seen ON replica(gw, updated_at)")
        conn.executemany("INSERT INTO replica VALUES ('hub', 'tasks', ?, 1, ?, ?, 0)",
                         ((f"default/default/t_fill{n:06d}", n + 1, json.dumps(_filler_task(n)))
                          for n in range(rows)))
        conn.commit()
    return rows + 1


def _build_board(tasks):
    """``tasks`` real board rows: one created by the API, the rest copies of it with fresh ids."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with contextlib.closing(kbc.connect()) as conn:
        seed = kb.create_task(conn, title="filler", assignee="default")
        kb.complete_task(conn, seed, summary="filler")
        cols = [row[1] for row in conn.execute("PRAGMA table_info(tasks)")]
        picked = ", ".join("?" if c == "id" else c for c in cols)
        with kb.write_txn(conn):
            conn.executemany(f"INSERT INTO tasks ({', '.join(cols)}) SELECT {picked} FROM tasks WHERE id = ?",
                             ((f"t_board{n:06d}", seed) for n in range(tasks - 1)))
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == tasks
    return kb.kanban_db_path(board=kb.DEFAULT_BOARD)


def _rf2_windows():
    """600 s of rf2 doubled: 80 task change windows, 568 row changes, 12 new done cards for the chats."""
    windows = [w for w in range(SECONDS // 2) if (w + 1) * 80 // 300 != w * 80 // 300]
    assert len(windows) == 80
    plan = {}
    for i, w in enumerate(windows):
        changes = (i + 1) * 568 // 80 - i * 568 // 80
        new = (i + 1) * 12 // 80 - i * 12 // 80
        plan[w] = (changes - new, new)
    assert sum(c + n for c, n in plan.values()) == 568 and sum(n for _, n in plan.values()) == 12
    return plan


def _lease_beat(lease_path):
    lease_path.write_text(json.dumps({"name": "kanban-remote-index", "held": True, "pid": os.getpid(),
                                      "start": None, "role": "gateway", "priority": 2, "interval": 5.0,
                                      "heartbeat_at": time.time()}))


def _skill_tree(root, skills, walked, dirs, files):
    """A skill root with the hub's live shape (``find -L``: SKILL.md count, directories the catalog walks,
    all directories, files), so ``skills.changed`` walks what the hub serve walks. ``walked`` excludes the
    support and cache directories the signature prunes (references/, scripts/, templates/, __pycache__/ under
    a skill: most of the hub's directories). Names are synthetic; the counts and the pruned names are the hub's."""
    from tools.skills_tool import _dir_tree_signature
    root.mkdir(parents=True)
    categories = min(max(1, skills // 9), walked - 1 - skills)
    skill_dirs = []
    for n in range(skills):
        skill = root / f"cat{n % categories:02d}" / f"skill{n:03d}"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: skill{n:03d}\ndescription: replay skill {n}\n---\nbody\n")
        skill_dirs.append(skill)
    for n in range(walked - 1 - categories - skills):  # walked non-support dirs inside skills (tests/, v/)
        (skill_dirs[n % skills] / f"tests{n}").mkdir()
    pruned = []
    for n in range(dirs - walked):  # one per support name per skill, then nested inside them
        slot, name = divmod(n, 4)
        if slot < skills:
            pruned.append(skill_dirs[slot] / ("references", "scripts", "templates", "__pycache__")[name])
            pruned[-1].mkdir()
        else:
            (pruned[n % len(pruned)] / f"sub{n}").mkdir()
    for n in range(files - skills):  # half beside SKILL.md, half in the pruned support dirs
        parent = skill_dirs[n % skills] if n % 2 or not pruned else pruned[n % len(pruned)]
        (parent / f"ref{n}.md").write_text("x\n")
    assert 1 + sum(1 for p in root.rglob("*") if p.is_dir()) == dirs
    assert sum(1 for p in root.rglob("*") if p.is_file()) == files
    rows = []
    _dir_tree_signature(str(root), rows)
    assert len(rows) == walked, (root, len(rows), walked)


@pytest.fixture
def row_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    # The hub's three skill roots (its own plus two external_dirs), counted with find -L on 2026-11-14:
    # SKILL.md files, directories outside pruned support/cache dirs, all directories, files.
    _skill_tree(home / "skills", 4, 18, 22, 119)
    _skill_tree(tmp_path / "mods-skills", 27, 49, 100, 315)
    _skill_tree(tmp_path / "state-skills", 187, 229, 468, 1487)
    (home / "config.yaml").write_text(
        f"display: {{}}\nskills:\n  external_dirs:\n    - {tmp_path / 'mods-skills'}\n"
        f"    - {tmp_path / 'state-skills'}\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home / "kanban"))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub"))
    (tmp_path / "hub").mkdir()
    token = set_hermes_home_override(str(home))
    monkeypatch.setattr(server, "_hermes_home", str(home))
    monkeypatch.setattr(server, "_served_profile_homes", ())
    monkeypatch.setattr(server, "_cfg_cache", None)
    for name in ("_change_sigs", "_change_checked_at", "_change_broadcast_at",
                 "_sessions_db_sig_cache", "_sessions_digest_at", "_sessions_db_identity",
                 "_room_store_sig_cache", "_groups_woken_heads", "_skin_sig_by_home"):
        monkeypatch.setattr(server, name, {})
    peers = [RecordingTransport() for _ in range(10)]
    monkeypatch.setattr(server, "_live_transports", set(peers))
    watch_store.reset()
    from tui_gateway import kanban_remote_notices as krn
    krn._reset_for_tests()
    try:
        yield home, peers
    finally:
        krn._reset_for_tests()
        watch_store.reset()
        reset_hermes_home_override(token)


def _seed(home, cell, wal_holders):
    """The cell's stores: sessions, the two chats, the board, the replica and the holder's first copy."""
    from hermes_cli.host_loop_lease import lease_dir
    from tui_gateway import kanban_remote_index as kri
    from tui_gateway import kanban_remote_notices as krn

    state = home / "state.db"
    owner = SessionDB(state)
    with contextlib.closing(sqlite3.connect(state)) as writer:
        writer.executemany("INSERT INTO sessions(id, source, started_at) VALUES (?, 'cli', 1)",
                           [(f"s{n}",) for n in range(cell["sessions"])])
        writer.commit()
    for chat in CHATS:
        owner.create_session(session_id=chat, source="cli")
    board = _build_board(cell["tasks"])
    replica = krn.hub_db_path()
    seq = _build_replica(replica, cell["replicas"])
    ledger = krn.ledger_path()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    for path in (state, board, replica):
        _pad_wal(path, wal_holders) if cell["large_wal"] else _checkpoint(path)
    lease = lease_dir() / f"{kri.LEASE}.lock"
    lease.parent.mkdir(parents=True, exist_ok=True)
    _lease_beat(lease)
    assert kri.refresh(replica, ledger)  # the holder's copy of the starting state
    return SimpleNamespace(state=state, owner=owner, board=board, replica=replica, ledger=ledger, lease=lease,
                           seq=seq)


def _instrument(monkeypatch, home, clock, reader_defers):
    """Virtual clock at the waits, the model turn replaced by a recorder, and CPU attribution wrappers."""
    from hermes_cli import loop_accounting
    from tui_gateway import kanban_remote_index as kri

    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(kri, "_clock", lambda: clock[0])
    monkeypatch.setattr(server, "_launch_home", home)
    ins = SimpleNamespace(errors=[], delivered=[], computes=Counter(), calls=Counter(), cpu_by_name=Counter())
    monkeypatch.setattr(server, "_notif_log_failure", lambda *args: ins.errors.append(repr(args)))

    def model_turn(rid, sid, session, text, **kwargs):  # the only replaced boundary: the agent turn
        ins.delivered.append((clock[0], sid, text))
        server._notif_release_turn(session)
        return True

    monkeypatch.setattr(server, "_notif_run_submit", model_turn)
    real_compute = kri.compute

    def compute(*args, **kwargs):
        ins.computes["reader" if threading.current_thread().name.startswith("replay-") else "holder"] += 1
        return real_compute(*args, **kwargs)

    monkeypatch.setattr(kri, "compute", compute)
    if not reader_defers:
        monkeypatch.setattr(kri, "defer", lambda *a, **k: False)  # RED control: the pre-fix reader
    real_timed = loop_accounting.timed

    def timed(name, fn, *args, **kwargs):
        ins.calls[name] += 1
        started = time.thread_time()
        try:
            return real_timed(name, fn, *args, **kwargs)
        finally:
            ins.cpu_by_name[name] += time.thread_time() - started

    monkeypatch.setattr(loop_accounting, "timed", timed)
    real_remote = server._collect_remote_kanban_notifications

    def remote(*args, **kwargs):  # attribution only: the remote half of poll_kanban
        started = time.thread_time()
        try:
            return real_remote(*args, **kwargs)
        finally:
            ins.cpu_by_name["poll_kanban.remote_collect"] += time.thread_time() - started

    monkeypatch.setattr(server, "_collect_remote_kanban_notifications", remote)
    return ins


def _complete_local(chat, title, summary):
    """A real local card with a TUI notify subscription for ``chat``, completed."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn

    with contextlib.closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title=title, assignee="default")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=chat)
        kb.complete_task(conn, tid, summary=summary)
    return tid


def _holder_pass(cx, now, measure):
    """The holder's 1 s pass: beat, and republish once at least 1 s after the first unpublished commit."""
    from tui_gateway import kanban_remote_index as kri

    _lease_beat(cx.seed.lease)
    if cx.stale_since is not None and now - cx.stale_since >= 1.0:
        started = time.thread_time()
        assert kri.refresh(cx.seed.replica, cx.seed.ledger)
        if measure:
            cx.holder_cpu += time.thread_time() - started
        cx.stale_since = None


def _replica_commit(cx, window, now, replicas):
    """One rf2 change window: ``restamps`` filler rows re-stamped plus ``new`` done cards for the chats."""
    restamps, new = cx.plan[window]
    for r in range(restamps):
        n = (window * 37 + r * 101) % replicas
        task = _filler_task(n)
        task["run"]["ended_at"] = now
        cx.seed.seq += 1
        cx.rwriter.execute("UPDATE replica SET seq = ?, data = ? WHERE coll = 'tasks' AND key = ?",
                           (cx.seed.seq, json.dumps(task), f"default/default/t_fill{n:06d}"))
    for _ in range(new):
        tid = f"t_remote{cx.counts['remote']:03d}"
        key, task = _done_card(tid, CHATS[cx.counts["remote"] % POLLERS], time.time() - 30)
        cx.seed.seq += 1
        cx.rwriter.execute("INSERT INTO replica VALUES ('hub', 'tasks', ?, 1, ?, ?, 0)",
                           (key, cx.seed.seq, json.dumps(task)))
        cx.commits[tid] = now
        cx.counts["remote"] += 1
    cx.rwriter.commit()
    cx.counts["replica_rows"] += restamps + new
    cx.stale_since = cx.stale_since if cx.stale_since is not None else now


def _warm_tick(cx, tick, warm_local):
    """Warm-up (not measured): a live serve is minutes old when P1 samples it, so one remote and one local
    notice per chat are delivered first; that builds the per-process replica projections the delivery path
    reads (a one-off ~1.8 s at 10k rows), as they were long built in the live process."""
    now = 1000.0 + (tick + 1) * TICK
    if tick == 1:
        for n, chat in enumerate(CHATS[:POLLERS]):
            key, task = _done_card(f"t_warmr{n:03d}", chat, time.time() - 30)
            cx.seed.seq += 1
            cx.rwriter.execute("INSERT INTO replica VALUES ('hub', 'tasks', ?, 1, ?, ?, 0)",
                               (key, cx.seed.seq, json.dumps(task)))
        cx.rwriter.commit()
        cx.stale_since = now
    if tick == 3:
        for n, chat in enumerate(CHATS[:POLLERS]):
            warm_local.append(_complete_local(chat, f"warm {n}", f"warm done {n}"))
    cx.clock[0] = now
    if now == int(now):
        _holder_pass(cx, now, measure=False)
    _watch_pass(now)
    cx.scheduler.advance(now)


def _measured_tick(cx, tick, start, replicas):
    now = start + (tick + 1) * TICK
    if tick % 4 == 0 and _state_stream(cx.swriter, tick // 4):
        cx.counts["state"] += 1
    if tick % 4 == 1 and tick // 4 in cx.plan:  # replica commit inside the 2 s window
        _replica_commit(cx, tick // 4, now, replicas)
    if tick in cx.local_due:
        n = cx.local_due[tick]
        cx.commits[_complete_local(CHATS[n % POLLERS], f"local {n}", f"local done {n}")] = now
        cx.counts["local"] += 1
    cx.clock[0] = now
    if now == int(now):  # the holder's 1 s pass
        _holder_pass(cx, now, measure=True)
    started = time.thread_time()
    _watch_pass(now)
    cx.watcher_cpu += time.thread_time() - started
    cx.scheduler.advance(now)


def _span_delta(before, after):
    """The production loop accounting over the window: the span names P1 reports, with their fallbacks."""
    spans = {}
    for name, end in after.items():
        begin = before.get(name, {})
        delta = {k: round(end[k] - begin.get(k, 0), 6) for k in ("runs", "skips", "changes", "fallbacks", "cpu_s")}
        if delta["runs"]:
            spans[name] = delta
    return spans


def _run_cell(home, peers, monkeypatch, cell, reader_defers=True):
    from hermes_cli import loop_accounting
    from tests.tui_gateway.j_nonrpc_scheduler import PollerScheduler
    from tools import process_registry, process_registry_awaiting
    from tools.skills_tool import skill_search_dirs
    from tui_gateway import kanban_remote_index as kri
    from tui_gateway.notification_wait import PollerStop

    wal_holders = []
    skill_roots = sorted(str(d) for _tier, d in skill_search_dirs()[0])
    assert {"mods-skills", "state-skills", "skills"} <= {os.path.basename(d) for d in skill_roots}, skill_roots
    seed = _seed(home, cell, wal_holders)
    clock = [1000.0]
    ins = _instrument(monkeypatch, home, clock, reader_defers)
    registry = process_registry.ProcessRegistry()
    monkeypatch.setattr(process_registry, "process_registry", registry)
    scheduler = PollerScheduler(clock, process_registry_awaiting.take_ready)
    monkeypatch.setattr(process_registry_awaiting, "take_ready", scheduler.take)
    cx = SimpleNamespace(seed=seed, clock=clock, scheduler=scheduler, plan=_rf2_windows(), commits={},
                         local_due={int((n + 0.5) * SECONDS / 12 / TICK): n for n in range(12)},  # evenly spaced
                         watcher_cpu=0.0, holder_cpu=0.0, stale_since=None, counts=Counter())
    sessions = []
    unguarded = contextlib.ExitStack()
    try:
        _watch_pass(clock[0])  # startup pass: first signatures, not part of the window
        for chat in CHATS[:POLLERS]:
            session = {"session_key": chat, "history_lock": threading.RLock(), "profile_home": str(home)}
            sessions.append(session)
            scheduler.start(server._notification_poller_scoped_loop, PollerStop(registry.completion_queue),
                            chat, session)
        with contextlib.closing(sqlite3.connect(seed.state)) as cx.swriter, \
                contextlib.closing(sqlite3.connect(seed.replica)) as cx.rwriter:
            warm_local = []
            for tick in range(WARM_TICKS):
                _warm_tick(cx, tick, warm_local)
            warm = [f"t_warmr{n:03d}" for n in range(POLLERS)] + warm_local
            missing = [tid for tid in warm if not any(tid in text for _at, _sid, text in ins.delivered)]
            assert not missing, (missing, ins.delivered)  # every warm-up notice reached its chat
            assert ins.computes.get("reader", 0) == 0, ins.computes
            for bucket in (ins.delivered, ins.calls, ins.cpu_by_name, ins.computes):
                bucket.clear()
            kri.stats["deferred"] = 0
            warm_samples = {ident: len(values) for ident, values in scheduler.samples.items()}
            for peer in peers:
                peer.frames.clear()
            start = clock[0]
            unguarded.enter_context(_production_io())  # the measured window only
            spans_before, load_before = loop_accounting.counters(), os.getloadavg()
            for tick in range(int(SECONDS / TICK)):
                _measured_tick(cx, tick, start, cell["replicas"])
            spans_after, load_after = loop_accounting.counters(), os.getloadavg()
    finally:
        unguarded.close()
        scheduler.close()
        seed.owner.close()
        wal_bytes = {p.name: os.path.getsize(f"{p}-wal") if os.path.exists(f"{p}-wal") else 0
                     for p in (seed.state, seed.board, seed.replica)}
        for conn in wal_holders:
            conn.close()
    # Each sample is one pass's CPU, recorded when the poller next blocks; the warm-up's are excluded.
    poller_cpu = sum(sum(values[warm_samples[ident]:]) for ident, values in scheduler.samples.items())
    latency = {}
    for at, _sid, text in ins.delivered:
        for tid, committed in cx.commits.items():
            if tid in text:
                latency.setdefault(tid, at - committed)
    spans = _span_delta(spans_before, spans_after)
    evidence = {"cell": cell, "reader_defers": reader_defers, "seconds": SECONDS, "pollers": POLLERS,
                "spans": spans, "loadavg_1m": [load_before[0], load_after[0]],
                "counts": dict(cx.counts), "calls": dict(ins.calls), "cpu_by_name_s": dict(ins.cpu_by_name),
                "computes": dict(ins.computes),
                "deferred": kri.stats.get("deferred"), "watcher_cpu_s": cx.watcher_cpu, "poller_cpu_s": poller_cpu,
                "holder_cpu_s": cx.holder_cpu, "row_core": (cx.watcher_cpu + poller_cpu) / SECONDS,
                "holder_core": cx.holder_cpu / SECONDS, "latency_s": latency,
                "max_latency_s": max(latency.values()) if latency else None,
                "frames_per_peer": [len(peer.frames) for peer in peers],
                "watcher_events_per_peer": [dict(Counter(frame["params"]["type"] for frame in peer.frames))
                                            for peer in peers], "wal_bytes": wal_bytes,
                "skill_roots": len(skill_roots)}
    _write_evidence(evidence)
    # Measured chg1 + notice stream: 83 message + 2 title updates and 24 persisted
    # notices yield 104 sessions.changed broadcasts (five notice updates coalesce
    # with chg1 under the 2 s floor). The pre-fix reader delivers notices sooner:
    # only three coalesce, yielding 106. The 55 activity-only commits stay silent;
    # warm-up frames were cleared and the other watched inputs stay unchanged.
    # All ten peers must receive the work whose CPU we charge, not just peer zero.
    expected_frames = 104 if reader_defers else 106
    assert len(peers) == 10, evidence
    assert evidence["watcher_events_per_peer"] == [{"sessions.changed": expected_frames}] * 10, evidence
    assert evidence["frames_per_peer"] == [expected_frames] * 10, evidence
    assert not ins.errors, (ins.errors, evidence)
    assert not scheduler.errors
    if cell["large_wal"]:  # the WAL stayed past 400 MiB through the measured window
        assert all(size >= WAL_BYTES for size in wal_bytes.values()), evidence
    assert cx.counts["state"] == 140 and cx.counts["replica_rows"] == 568
    assert cx.counts["remote"] == 12 and cx.counts["local"] == 12
    assert set(latency) == set(cx.commits), (sorted(set(cx.commits) - set(latency)), evidence)
    assert all(not s.get("_kanban_claims") and not s.get("_kanban_pending") for s in sessions), evidence
    # P1 fallback ceiling: run11 (cardJ/run11/p1-r11.json) records 0 fallbacks on every change_watcher/*,
    # notifications.* and remote_index.* span in serve and gateway, so the replay allows none either.
    assert {name: s["fallbacks"] for name, s in spans.items() if s["fallbacks"]} == {}, evidence
    return evidence


def _write_evidence(evidence):
    """Raw per-cell evidence, appended before any assertion so a failing cell still leaves its numbers:
    the runner rewrites the junit file when it reruns failed ids. Set J_ROWS_EVIDENCE to a .jsonl path."""
    path = os.environ.get("J_ROWS_EVIDENCE")
    if path:  # an operator-chosen path outside the isolated home: the guard's original open
        with getattr(builtins.open, "__wrapped__", builtins.open)(path, "a", encoding="utf-8") as out:
            out.write(json.dumps(evidence, sort_keys=True) + "\n")


def _watch_pass(now):
    server._broadcast_skin_if_changed()
    server._broadcast_watched_changes(now=now)
    server._wake_room_drivers()


def _state_stream(writer, window):
    """chg1, count-preserving: 140 state commits spread over 300 windows (83 message, 2 title, 55 activity)."""
    ordinal = (window + 1) * 140 // 300
    if ordinal == window * 140 // 300:
        return None
    kind = "message" if ordinal <= 83 else "title" if ordinal <= 85 else "activity"
    column = {"message": "message_count", "title": "title", "activity": "last_activity_at"}[kind]
    writer.execute(f"UPDATE sessions SET {column}=? WHERE id='s0'", (ordinal,))
    writer.commit()
    return kind


@pytest.mark.parametrize("name", list(CELLS))
def test_complete_serve_and_gateway_rows(row_home, monkeypatch, record_property, name):
    home, peers = row_home
    evidence = _run_cell(home, peers, monkeypatch, CELLS[name])
    _RESULTS[name] = evidence
    record_property("j_row", json.dumps(evidence, sort_keys=True))
    # Every notice arrives; a deferral costs at most one 5 s poll after the holder's ~1-2 s publish.
    assert evidence["max_latency_s"] <= 2 * 5.0 + 2.0, evidence
    assert evidence["computes"].get("reader", 0) == 0, evidence  # the holder publishes within the bound
    assert evidence["row_core"] <= 0.007, evidence  # serve row; the gateway row (0.008) is looser
    if name != "base":
        base = _RESULTS.get("base")
        assert base is not None, "run the whole parametrization: growth compares with the base cell"
        assert evidence["row_core"] / base["row_core"] <= 1.5, (evidence, base)


def test_red_control_pre_fix_reader_rebuilds_on_misses(row_home, monkeypatch, record_property):
    """The same base replay with the reader's deferral removed: polls between a commit and the holder's
    publish rebuild the index in the poller (P1's residual)."""
    home, peers = row_home
    evidence = _run_cell(home, peers, monkeypatch, BASE, reader_defers=False)
    record_property("j_row_red", json.dumps(evidence, sort_keys=True))
    assert evidence["computes"].get("reader", 0) > 0, evidence
    if "base" in _RESULTS:
        assert evidence["poller_cpu_s"] > _RESULTS["base"]["poller_cpu_s"], (evidence, _RESULTS["base"])
