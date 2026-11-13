"""Write-overhead benchmark for the state.db session change cursor (card D1, design t_01a1d563324d7e8e §2 P3).

Run: scripts/run-in-hermes-env python scripts/benchmark_session_change_overhead.py [rounds] [inserts_per_round]

The hot write path is a message insert: ``append_message`` inserts the row (FTS triggers included) and bumps
``sessions.message_count``. The cursor's ``message_count`` trigger evaluates one WHEN on that update and runs
its body only on a 0 <-> non-0 crossing, so steady-state inserts should cost the same with or without it.

Each round times three adjacent blocks on the same file: cursor ON (triggers installed), OFF (triggers
dropped) and OFF again, in a rotating order. The ON/OFF ratio per round is the overhead and the OFF/OFF
ratio is an A/A control showing how far this host moves with nothing changed; the reported number is the
median over rounds, so drift (page cache, WAL growth, checkpoints, neighbours) cancels. Two paths:

- ``append_message``: the real SessionDB call; its wall time per insert is the write latency a turn pays.
  Its process CPU is printed too: the triggers' only steady-state cost is compiling their programs into
  the ``message_count`` UPDATE, which the writer re-prepares per write (a few percent of CPU, under 1%
  of latency, since the commit's fsync dominates).
- ``raw sql``: the same two statements in one transaction on a plain connection with
  ``synchronous=OFF`` and cached statements, so its wall time is statement execution only.

Exits 1 when either gated (wall) median overhead is above ``LIMIT_PCT`` (3%).
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
PER_ROUND = int(sys.argv[2]) if len(sys.argv) > 2 else 150
SESSIONS = 2000
LIMIT_PCT = 3.0

tmp = Path(tempfile.mkdtemp(prefix="session-change-bench-", dir=os.environ.get("TMPDIR")))
os.environ["HERMES_HOME"] = str(tmp)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hermes_state_session_change as sc
from hermes_state import SessionDB

db = SessionDB(db_path=tmp / "state.db")
hot = [f"hot{i}" for i in range(20)]


def seed() -> None:
    def write(conn):
        conn.executemany("INSERT INTO sessions (id, source, started_at, title) VALUES (?, 'cli', ?, ?)",
                         [(f"s{i}", 1_700_000_000.0 + i, f"session {i}") for i in range(SESSIONS)])
    db._execute_write(write)
    for sid in hot:
        db.create_session(sid, "cli")
        db.append_message(sid, "user", "warm up")  # past 0 -> 1: steady-state inserts from here


def set_cursor(on: bool) -> None:
    def write(conn):
        if on:
            sc.ensure_installed(conn)
        else:
            sc.drop_triggers(conn)
    db._execute_write(write)
    with db._read_ctx() as conn:
        installed = bool(sc._installed(conn))
    assert installed is on


def time_append(n: int) -> tuple[float, float]:
    body = "x" * 400
    wall, cpu = time.perf_counter(), time.process_time()
    for i in range(n):
        db.append_message(hot[i % len(hot)], "assistant" if i % 2 else "user", f"{body} {i}")
    return (time.perf_counter() - wall) / n, (time.process_time() - cpu) / n


def time_raw(conn: sqlite3.Connection, n: int) -> tuple[float, float]:
    wall, cpu = time.perf_counter(), time.process_time()
    for i in range(n):
        sid = hot[i % len(hot)]
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
                     (sid, f"raw {i}", time.time()))
        conn.execute("UPDATE sessions SET message_count = message_count + 1 WHERE id = ?", (sid,))
        conn.execute("COMMIT")
    return (time.perf_counter() - wall) / n, (time.process_time() - cpu) / n


def main() -> int:
    seed()
    raw = sqlite3.connect(tmp / "state.db", isolation_level=None, timeout=30)  # health: allow HX015 -- throwaway benchmark file in a temp dir
    raw.execute("PRAGMA synchronous = OFF")  # statement cost only, no fsync noise
    # Per round: ON, OFF and a second OFF (an A/A control), in a rotating order, so each ratio compares
    # adjacent blocks and the control shows how much this host moves with no change at all.
    ratios: dict[tuple[str, int, str], list[float]] = {}
    orders = (("on", "off", "off2"), ("off", "off2", "on"), ("off2", "on", "off"))
    for r in range(ROUNDS):
        got: dict[str, tuple] = {}
        for side in orders[r % 3]:
            set_cursor(side == "on")
            got[side] = (time_append(PER_ROUND), time_raw(raw, PER_ROUND))
        for p, path in enumerate(("append_message", "raw sql")):
            for i in (0, 1):
                ratios.setdefault((path, i, "on"), []).append(got["on"][p][i] / got["off"][p][i])
                ratios.setdefault((path, i, "a/a"), []).append(got["off2"][p][i] / got["off"][p][i])
    raw.close()
    db.close()

    def pct(path: str, i: int, kind: str) -> float:
        return (statistics.median(ratios[(path, i, kind)]) - 1.0) * 100.0

    worst = 0.0
    for path, gated in (("append_message", 0), ("raw sql", 0)):
        for i, name in ((0, "wall"), (1, "cpu")):
            over, control = pct(path, i, "on"), pct(path, i, "a/a")
            mark = "  <- gated" if i == gated else ""
            if i == gated:
                worst = max(worst, over)
            print(f"{path:15s} {name:4s} cursor overhead {over:+6.2f}%   (A/A control {control:+6.2f}%){mark}")
    print(f"{ROUNDS} rounds x {PER_ROUND} inserts per block, median of paired ratios; limit {LIMIT_PCT:.1f}%: "
          f"{'PASS' if worst <= LIMIT_PCT else 'FAIL'} (worst gated {worst:+.2f}%)")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if worst <= LIMIT_PCT else 1


if __name__ == "__main__":
    sys.exit(main())
