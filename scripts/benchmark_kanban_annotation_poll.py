"""Synthetic benchmark: annotation + links refresh CPU per 3 s poll under 1 heartbeat/s on one busy board.

Run: scripts/run-in-hermes-env python scripts/benchmark_kanban_annotation_poll.py [boards] [polls] (t_01a1cee98aaa8db1)
Prints process CPU per poll (time.process_time) and read counters. Same harness for before/after: on a base checkout
without kanban_links_narrow the projector simply declares nothing.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

N_BOARDS = int(sys.argv[1]) if len(sys.argv) > 1 else 14
POLLS = int(sys.argv[2]) if len(sys.argv) > 2 else 40

tmp = Path(tempfile.mkdtemp(prefix="kanban-annotation-bench-", dir=os.environ.get("TMPDIR")))
home = tmp / ".hermes"
home.mkdir()
os.environ["HERMES_HOME"] = str(home)
os.environ["HERMES_KANBAN_HOME"] = str(home)
os.environ["HERMES_HUB_STATE"] = str(tmp / "hub-state")
os.environ.setdefault("HERMES_ALLOW_KANBAN_TEST_DB", "1")

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_session_activity as ksa
from hermes_cli.kanban_db_dispatch import heartbeat_worker

paths = [home / "kanban.db"] + [home / "kanban" / "boards" / f"b{i}" / "kanban.db" for i in range(1, N_BOARDS)]
conns = []
for i, p in enumerate(paths):
    p.parent.mkdir(parents=True, exist_ok=True)
    c = kbc.connect(p)
    for j in range(60):  # open, done, and done-in-window cards per board
        tid = kb.create_task(c, title=f"card {j} (split to follow-up)" if j % 7 == 0 else f"card {j}",
                             assignee="w", session_id=f"s_{i}_{j % 9}")
        if j % 3 == 0:
            kb.claim_task(c, tid)
            kb.complete_task(c, tid, result="ok")
    conns.append(c)
busy = conns[0]
running = kb.create_task(busy, title="worker", assignee="w", session_id="s_busy")
kb.claim_task(busy, running)

ksa._board_db_paths = lambda: list(paths)
ksa._replica_paths = list
ksa._hub_db_path = lambda: None
ksa._origin_scope = lambda: ("gw", "default")

projections = [0]
recent_reads = [0]
board_reads = [0]
LOCAL_ROWS = [{"gateway": "gw", "profile": "default", "origin_gateway": "gw", "origin_profile": "default",
               "board": ksa._board_slug(paths[3]), "id": "x"}]


def project():
    """Cost model of the real projection: read every task row of the boards its rows reference, plus decode work."""
    projections[0] += 1
    sink = getattr(ksa._origin_rows_sink, "rows", None)
    if sink is not None:
        sink.extend(LOCAL_ROWS)
    out = []
    for p in paths:  # the real projection decodes the ledger + replica (~2.4k rows on the hub)
        with kbc.connect(p) as c:
            out.extend(dict(r) for r in c.execute("SELECT id, status, title FROM tasks"))
    return [dict(r, session="s_remote") for r in out[:5]]


try:
    from hermes_cli import kanban_links_narrow as narrow
    setattr(project, narrow.DECLARES_BOARD_READS, True)
    mode = "after"
except ImportError:
    mode = "before"
ksa._project_remote_links = project
real_recent, real_board = ksa._read_recent_done, ksa._read_board
ksa._read_recent_done = lambda *a: recent_reads.__setitem__(0, recent_reads[0] + 1) or real_recent(*a)
ksa._read_board = lambda p, **kw: board_reads.__setitem__(0, board_reads[0] + 1) or real_board(p, **kw)
ksa.register_done_check("bench", lambda card: "split" in (card.get("title") or ""))
ksa._reset_cache_for_tests()


def poll():
    ksa._attention.clear()  # past the attention TTL each poll (the hub's 3 s polls outlive it under churn)
    with ksa._cache_lock:
        ksa._cache = None  # likewise the counts TTL: measure the recompute path, not the TTL hit
    ksa.active_task_counts()
    ksa.attention_task_counts()
    ksa._remote_links()


poll()  # warm
for k in (projections, recent_reads, board_reads):
    k[0] = 0
cpu = 0.0
for i in range(POLLS):
    for _ in range(3):  # 3 s between polls at 1 heartbeat/s on the busy board
        heartbeat_worker(busy, running)
    if i % 10 == 9:  # plus a real commit on the busy board every 30 s
        kb.add_comment(busy, running, "bench", "progress") if hasattr(kb, "add_comment") else None
    t0 = time.process_time()
    poll()
    cpu += time.process_time() - t0
print(f"{mode}: boards={N_BOARDS} polls={POLLS} cpu/poll={cpu / POLLS * 1000:.2f} ms "
      f"projections={projections[0]} recent_done_reads={recent_reads[0]} board_count_reads={board_reads[0]}")
