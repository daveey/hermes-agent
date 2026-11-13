"""Replay benchmark for the dispatcher reclaim phase (G2, design t_01a1d563324d7e8e): real ``dispatch_once``
ticks over a COPY of a board in a scratch home, CPU per tick per sweep from ``loop_accounting``.

usage: scripts/hermes-python scripts/kanban_reclaim_replay_bench.py <worktree> <board copy> <ticks>
           [--churn N | --replay SECONDS] [--json out] [--profile]

--churn N       before every Nth tick, one content write (a comment), so the board revision moves; 0 = write-free.
--replay S      replay the copy's own write cadence: tick i writes iff the i-th 15 s bucket of the last S seconds
                of the board's history had a content event (heartbeats excluded: they never move the revision).

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
    if k.startswith("HERMES_KANBAN") or k.startswith("HERMES_SESSION"):
        del os.environ[k]
shutil.copy(src, os.path.join(home, "kanban.db"))
Path(home, "config.yaml").write_text(f"kanban:\n  dispatch_interval_seconds: {INTERVAL}\n", encoding="utf-8")
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
res = {}
for k, v in la.counters().items():
    if k.startswith("dispatch"):
        b = base.get(k, {})
        res[k] = {"runs": v["runs"] - b.get("runs", 0), "skips": v["skips"] - b.get("skips", 0),
                  "cpu_ms_per_tick": round(1000 * (v["cpu_s"] - b.get("cpu_s", 0.0)) / ticks, 3)}
summary = {"board": os.path.basename(src), "ticks": ticks, "churn": churn, "replay_s": replay,
           "write_ticks": writes, "cpu_ms_per_tick_total": round(1000 * cpu / ticks, 3),
           "core_pct_at_interval": round(100 * cpu / ticks / INTERVAL, 3), "wall_s": round(wall, 2), "sweeps": res}
print(json.dumps(summary, indent=1))
if out:
    with open(out, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
shutil.rmtree(home, ignore_errors=True)
