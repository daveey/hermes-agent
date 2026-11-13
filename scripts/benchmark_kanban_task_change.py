#!/usr/bin/env python3
"""Benchmark the per-task change cursor (P2, t_01a1d59d3ef805c4). [daveey]

1. Write overhead: one bumping write (a task title UPDATE in its own write transaction, as kanban_db commits) with the
   epoch-1 bump-only triggers vs the epoch-2 bump+mark triggers, on the same file, alternating blocks.
2. Scale: ``changed_since`` for a fixed change (one task touched) at 1k, 5.8k and 20k tasks. Its cost must not grow
   with the board (the design's bar: <= 1.5x from 1k to 20k).

    bash scripts/run-in-hermes-env python3 scripts/benchmark_kanban_task_change.py [--json OUT]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _home() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="kanban-task-change-bench-"))
    home = tmp / ".hermes"
    home.mkdir()
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_KANBAN_HOME"] = str(home)
    return home


def _seed(conn, n: int) -> list[str]:
    from hermes_cli.kanban_db_connect import write_txn
    ids = []
    with write_txn(conn):
        for i in range(n):
            tid = f"t_bench{i:08d}"
            conn.execute("INSERT INTO tasks (id, title, status, assignee, created_at) VALUES (?, ?, 'ready', 'w', ?)",
                         (tid, f"task {i}", i))
            conn.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES (?, 'created', ?)", (tid, i))
            conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'me', 'hi', ?)",
                         (tid, i))
            if i:
                conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (ids[-1], tid))
            ids.append(tid)
    return ids


def _epoch1(conn) -> None:
    """The bump-only triggers a writer from before P2 installs (same WHEN clauses, no marks)."""
    from hermes_cli import kanban_activity_revision as rev
    from hermes_cli.kanban_db_connect import write_txn
    found = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name GLOB ?",
                              (rev.PREFIX + "*",)))
    with write_txn(conn):
        for name, sql in found.items():
            conn.execute(f"DROP TRIGGER {name}")
            conn.execute(sql.split(" BEGIN ", 1)[0] + f" BEGIN {rev._BUMP} END")


def _write_cost(conn, ids: list[str], n: int) -> float:
    from hermes_cli.kanban_db_connect import write_txn
    samples = []
    for i in range(n):
        t0 = time.perf_counter()
        with write_txn(conn):
            conn.execute("UPDATE tasks SET title = ? WHERE id = ?", (f"w{time.perf_counter_ns()}", ids[i % len(ids)]))
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples)


def _statement_cost(conn, ids: list[str], n: int) -> float:
    """The trigger's CPU share alone: n bumping UPDATEs inside one transaction (no commit/fsync per write)."""
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):
        t0 = time.perf_counter()
        for i in range(n):
            conn.execute("UPDATE tasks SET title = ? WHERE id = ?", (f"s{i}-{time.perf_counter_ns()}", ids[i % len(ids)]))
        return (time.perf_counter() - t0) / n


def bench_overhead(blocks: int, per_block: int) -> dict:
    from hermes_cli import kanban_activity_revision as rev
    from hermes_cli import kanban_db_connect as kbc
    home = _home()
    conn = kbc.connect(home / "kanban.db")
    ids = _seed(conn, 1000)
    old, new, old_s, new_s = [], [], [], []
    for _ in range(blocks):
        _epoch1(conn)
        old.append(_write_cost(conn, ids, per_block))
        old_s.append(_statement_cost(conn, ids, per_block * 5))
        rev.ensure_revision(conn, rev.LOCAL_TABLES)
        new.append(_write_cost(conn, ids, per_block))
        new_s.append(_statement_cost(conn, ids, per_block * 5))
    conn.close()
    o, w = statistics.median(old), statistics.median(new)
    so, sw = statistics.median(old_s), statistics.median(new_s)
    return {"bump_only_us": round(o * 1e6, 2), "bump_and_mark_us": round(w * 1e6, 2),
            "overhead_pct": round((w / o - 1) * 100, 2),
            "statement_bump_only_us": round(so * 1e6, 2), "statement_bump_and_mark_us": round(sw * 1e6, 2),
            "statement_overhead_pct": round((sw / so - 1) * 100, 2),
            "blocks": blocks, "writes_per_block": per_block}


def bench_scale(sizes: list[int], reps: int) -> dict:
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_task_change as tc
    from hermes_cli.kanban_db_connect import write_txn
    out = {}
    for n in sizes:
        home = _home()
        conn = kbc.connect(home / "kanban.db")
        ids = _seed(conn, n)
        first, cursor = tc.changed_since(conn, None)
        assert first is tc.FULL and cursor is not None
        samples = []
        for i in range(reps):
            with write_txn(conn):
                conn.execute("UPDATE tasks SET title = ? WHERE id = ?", (f"r{i}", ids[(i * 7919) % n]))
            t0 = time.perf_counter()
            changed, nxt = tc.changed_since(conn, cursor)
            samples.append(time.perf_counter() - t0)
            assert changed == {ids[(i * 7919) % n]}, changed
            cursor = nxt
        full_t0 = time.perf_counter()
        conn.execute("SELECT * FROM tasks").fetchall()
        full = time.perf_counter() - full_t0
        out[n] = {"changed_since_us": round(statistics.median(samples) * 1e6, 2),
                  "p90_us": round(sorted(samples)[int(len(samples) * 0.9)] * 1e6, 2),
                  "full_tasks_read_us": round(full * 1e6, 2)}
        conn.close()
    base = out[sizes[0]]["changed_since_us"]
    return {"sizes": out, "growth_max_over_min_size": round(out[sizes[-1]]["changed_since_us"] / base, 3)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    ap.add_argument("--blocks", type=int, default=12)
    ap.add_argument("--per-block", type=int, default=200)
    ap.add_argument("--reps", type=int, default=400)
    args = ap.parse_args()
    result = {"overhead": bench_overhead(args.blocks, args.per_block),
              "scale": bench_scale([1000, 5800, 20000], args.reps)}
    text = json.dumps(result, indent=2)
    print(text)
    if args.json:
        Path(args.json).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
