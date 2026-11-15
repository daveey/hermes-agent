"""Replay benchmark for the dispatcher reclaim phase (G2, design t_01a1d563324d7e8e): real ``dispatch_once``
ticks over a COPY of a board in a scratch home, CPU per tick per sweep from ``loop_accounting``.

usage: scripts/hermes-python scripts/kanban_reclaim_replay_bench.py <worktree> <board copy> <ticks>
           [--churn N | --replay SECONDS | --phases] [--blockers N] [--json out] [--profile]

--churn N       before every Nth tick, one content write (a comment), so the board revision moves; 0 = write-free.
--replay S      replay the copy's own write cadence: tick i writes iff the i-th 15 s bucket of the last S seconds
                of the board's history had a content event (heartbeats excluded: they never move the revision).
--blockers N    after the ticks, N dirty-tick passes of ``sweep_shared_blockers`` alone (a comment write before each
                pass), reported as ``dirty_shared_blockers`` median/min/max CPU ms per pass (G2c).
--phases        dirty-tick case: instead of whole ticks, <ticks> rounds of (one comment write, then one reclaim-phase
                entry point: apply_remote_archives, wake_due_continuations, recompute_ready, sweep_shared_blockers),
                reporting each phase's median CPU per call. Every call sees a moved board revision.

Side effects are stubbed: wake-check probes, the CI sweep, the capability refresh, worker placement (Board),
spawns (max_spawn=0), signals and subprocesses; any DB path outside the scratch home opens an empty read-only file.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

INTERVAL = 15
wt, src, ticks = sys.argv[1], sys.argv[2], int(sys.argv[3])
churn = int(sys.argv[sys.argv.index("--churn") + 1]) if "--churn" in sys.argv else 0
replay = int(sys.argv[sys.argv.index("--replay") + 1]) if "--replay" in sys.argv else 0
out = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else None
blocker_ticks = int(sys.argv[sys.argv.index("--blockers") + 1]) if "--blockers" in sys.argv else 0
schedule = None
if replay:
    ro = sqlite3.connect(f"file:{os.path.abspath(src)}?mode=ro", uri=True)
    end = ro.execute("SELECT MAX(created_at) FROM task_events").fetchone()[0] or 0
    start = end - replay
    hit = {int(r[0]) for r in ro.execute(
        "SELECT DISTINCT (created_at - ?) / ? FROM task_events WHERE kind != 'heartbeat' AND created_at > ? "
        "UNION SELECT DISTINCT (created_at - ?) / ? FROM task_comments WHERE created_at > ?",
        (start, INTERVAL, start, start, INTERVAL, start))}
    ro.close()
    ticks = min(ticks, replay // INTERVAL)
    schedule = [i in hit for i in range(ticks)]
home = tempfile.mkdtemp(prefix="reclaim-bench-home-")
os.environ["HERMES_HOME"] = home
for k in list(os.environ):
    if k.startswith(("HERMES_KANBAN", "HERMES_SESSION", "HERMES_DELEGATED")):  # a delegated-child marker would stub the drain
        del os.environ[k]
shutil.copy(src, os.path.join(home, "kanban.db"))
sync_yaml = ""
if "--hub-sync" in sys.argv:
    # G2e: the hub is the Board (``kanban_sync_state`` gateway ``hub``, mode ``primary``) and keeps a sync-shadow replica of
    # its own board, so every tick's ``apply_incoming`` reads that replica. ``--hub-sync SHADOW`` plants a copy of one.
    shadow = sys.argv[sys.argv.index("--hub-sync") + 1]
    os.makedirs(os.path.join(home, "kanban", "sync-shadow"))
    shutil.copy(shadow, os.path.join(home, "kanban", "sync-shadow", "default.db"))
    _b = sqlite3.connect(os.path.join(home, "kanban.db"))
    _b.execute("UPDATE kanban_sync_state SET gateway_id = 'hub', mode = 'primary'")
    _b.commit()
    _b.close()
    sync_yaml = "  sync:\n    enabled: true\n"
Path(home, "config.yaml").write_text(f"kanban:\n  dispatch_interval_seconds: {INTERVAL}\n{sync_yaml}", encoding="utf-8")
sys.path.insert(0, wt)
_orig_connect = sqlite3.connect


def _guarded(db, *a, **k):
    p = str(db)
    if p.startswith("file:"):
        p = "/" + p[5:].split("?")[0].lstrip("/")
    if p not in (":memory:", "", "/:memory:") and not os.path.realpath(p).startswith(os.path.realpath(home)):
        k.pop("uri", None)  # a board row names a live-host DB: never open the real file
        return _orig_connect(f"file:{home}/absent-{os.path.basename(p)}?mode=ro", *a[1:], uri=True, **k)
    return _orig_connect(db, *a, **k)


sqlite3.connect = _guarded

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as d
from hermes_cli import loop_accounting as la
import hermes_cli.kanban_capability_registry as cr
import hermes_cli.kanban_ci_progress as ci
import hermes_cli.kanban_wake_check as kwc
import hermes_cli.kanban_worker_placement as wp
import subprocess


class _NoPool:
    def submit(self, *a, **k):
        raise RuntimeError("bench: probe spawn refused")


class _NoProc:
    def __init__(self, *a, **k):
        raise PermissionError("bench: subprocess refused")


_orig_kill = os.kill


def _no_killpg(*a):
    raise PermissionError("bench: killpg refused")


def _no_kill(pid, sig):
    if sig != 0:
        raise PermissionError(f"bench: signal {sig} to {pid} refused")
    return _orig_kill(pid, 0)


kwc._pool = lambda: _NoPool()
os.kill = _no_kill
os.__dict__["killpg"] = _no_killpg  # POSIX-only bench: refuse process-group signals
kb._host_prefix = lambda: "bench-nohost:"
kb._claimer_id = lambda: "bench-nohost:1"
subprocess.Popen = _NoProc
ci.submit_sweep = lambda *a, **k: None
cr.maybe_refresh_async = lambda **k: False
wp.dispatch_tick = lambda conn: None

conn = kbc.connect()
row = conn.execute("SELECT id FROM tasks WHERE status NOT IN ('done','archived') ORDER BY created_at DESC LIMIT 1")
t_open = (row.fetchone() or [None])[0]


def tick():
    d.dispatch_once(conn, spawn_fn=lambda *a, **k: None, max_spawn=0, board=None)


tick()  # warm: first tick installs triggers and imports
if "--hub-sync" in sys.argv:
    # The replica's history holds operator-call requests this fresh copy has never scanned: ``apply_incoming`` works through
    # them MAX_PER_TICK at a time. Drain that backlog before measuring, so the ticks below are the steady state.
    from hermes_cli import kanban_operator_call as _kox
    for _ in range(200):
        if not _kox.apply_incoming(conn):
            break
if "--phases" in sys.argv:
    # Dirty-tick case (G2c, t_01a1db079d8d4505): one content write before every round, then each reclaim-phase entry
    # point alone, CPU per call (``time.process_time``). Matches the G2b replay (t01a1d701_dirty.py), whose revision-
    # keyed memos missed on every such round.
    import statistics
    import types
    from hermes_cli.kanban_continuation import wake_due_continuations
    from hermes_cli.kanban_idle_candidates import sweep_shared_blockers
    from hermes_cli.kanban_owner_archive_relay import apply_remote_archives
    phases = {
        "apply_remote_archives": lambda: apply_remote_archives(conn),
        "wake_due_continuations": lambda: wake_due_continuations(conn),
        "recompute_ready": lambda: kb.recompute_ready(conn),
        "sweep_shared_blockers": lambda: sweep_shared_blockers(conn, types.SimpleNamespace()),
    }
    for fn in phases.values():  # warm each phase's memos
        fn()
    samples: dict = {name: [] for name in phases}
    for i in range(ticks):
        for name, fn in phases.items():
            if t_open:
                with kb.write_txn(conn):
                    kb._insert_comment(conn, t_open, "bench", f"dirty {i} {name}", int(time.time()))
            c = time.process_time()
            try:
                fn()
            except Exception as exc:  # health: allow BLE001 -- a stubbed side effect; the CPU spent still counts
                print(f"bench: {name}: {exc!r}"[:200], file=sys.stderr)
            samples[name].append(time.process_time() - c)
    summary = {"board": os.path.basename(src), "rounds": ticks, "dirty_phase_ms": {
        name: {"median": round(1000 * statistics.median(s), 3), "min": round(1000 * min(s), 3)}
        for name, s in samples.items()}}
    print(json.dumps(summary, indent=1))
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=1)
    shutil.rmtree(home, ignore_errors=True)
    sys.exit(0)
base = {k: dict(v) for k, v in la.counters().items()}
prof = None
if "--profile" in sys.argv:
    import cProfile
    prof = cProfile.Profile()
    prof.enable()
writes = 0
c0, w0 = time.thread_time(), time.perf_counter()
for i in range(ticks):
    write = schedule[i] if schedule is not None else bool(churn and i % churn == 0)
    if write and t_open:
        writes += 1
        with kb.write_txn(conn):
            kb._insert_comment(conn, t_open, "bench", f"churn {i}", int(time.time()))
    tick()
cpu, wall = time.thread_time() - c0, time.perf_counter() - w0
if prof is not None:
    import pstats
    prof.disable()
    pstats.Stats(prof).sort_stats("cumulative").print_stats("hermes_cli", 60)
    if os.environ.get("BENCH_CALLERS"):  # e.g. BENCH_CALLERS='blocker_states': who calls each entry
        pstats.Stats(prof).sort_stats("cumulative").print_callers(os.environ["BENCH_CALLERS"])
    if os.environ.get("BENCH_CALLEES"):  # e.g. BENCH_CALLEES='dispatcher_drain|apply_incoming': who each entry spends its time in
        pstats.Stats(prof).sort_stats("cumulative").print_callees(os.environ["BENCH_CALLEES"])
res = {}
for k, v in la.counters().items():
    if k.startswith("dispatch"):
        b = base.get(k, {})
        res[k] = {"runs": v["runs"] - b.get("runs", 0), "skips": v["skips"] - b.get("skips", 0),
                  "cpu_ms_per_tick": round(1000 * (v["cpu_s"] - b.get("cpu_s", 0.0)) / ticks, 3)}
blockers = None
if blocker_ticks:
    # G2c dirty-tick blocker case: one content write (a comment), then one ``sweep_shared_blockers`` pass (its three
    # lanes), so every pass misses the revision gates the way the hub's busy board does. CPU per pass, ms.
    from types import SimpleNamespace
    from hermes_cli.kanban_idle_candidates import sweep_shared_blockers
    samples = []
    for i in range(blocker_ticks):
        if t_open:
            with kb.write_txn(conn):
                kb._insert_comment(conn, t_open, "bench", f"blocker churn {i}", int(time.time()))
        b0 = time.thread_time()
        sweep_shared_blockers(conn, SimpleNamespace())
        samples.append(1000 * (time.thread_time() - b0))
    samples.sort()
    blockers = {"passes": blocker_ticks, "median_ms": round(samples[len(samples) // 2], 3),
                "min_ms": round(samples[0], 3), "max_ms": round(samples[-1], 3)}
summary = {"board": os.path.basename(src), "ticks": ticks, "churn": churn, "replay_s": replay,
           "write_ticks": writes, "cpu_ms_per_tick_total": round(1000 * cpu / ticks, 3),
           "core_pct_at_interval": round(100 * cpu / ticks / INTERVAL, 3), "wall_s": round(wall, 2), "sweeps": res}
if blockers is not None:
    summary["dirty_shared_blockers"] = blockers
print(json.dumps(summary, indent=1))
if out:
    with open(out, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
shutil.rmtree(home, ignore_errors=True)
