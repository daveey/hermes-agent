"""[daveey] D2 benchmark: ranked-id memo vs the exact ranking (design t_01a1d563324d7e8e §3 D2).

Scale: per-request CPU of the 200-row first page at 5k and 23k sessions for an unchanged store (served),
one mid-turn activity bump and one new message (both re-rank), against the exact path. The memo's growth from
5k to 23k must stay <= 1.5x (exit 1 otherwise); the exact path grows with the store.

Replay: a Desktop-shaped refresh (sessions page, sidebar recents/cron, project tree) every round, with a hub-like
write mix between refreshes (heartbeats, message appends, titles, archives, rare rewinds), run against both paths
on identically seeded stores. Reports CPU per refresh, the ratio and the memo's outcome mix (fallback rate).

    hermes-core-python scripts/benchmark_rank_memo.py [--sizes 5000,23000] [--rounds 60] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hermes_state_rank_memo as memo
import hermes_state_recency as recency
from hermes_state import SessionDB

BASE = 1_700_000_000.0
PROMPT = "You are a Kanban worker.\n" * 400  # ~10 KB first user message, like the hub's worker prompts
SLICES = (dict(limit=200, min_message_count=1, include_pinned=True),
          dict(limit=40, min_message_count=1, exclude_sources=["cron", "telegram"], include_pinned=True),
          dict(limit=20, min_message_count=1, source="cron"),
          dict(limit=2000, min_message_count=1))


def seed(db: SessionDB, n: int, rng: random.Random) -> list[str]:
    ids = [f"s{i:06d}" for i in range(n)]

    def write(conn):
        rows, msgs = [], []
        for i, sid in enumerate(ids):
            parent = ids[rng.randrange(0, i)] if i and rng.random() < 0.08 else None
            ts = BASE + i * 30
            rows.append((sid, rng.choice(("cli", "cli", "cron", "telegram", "kanban")), ts,
                         ts + rng.randrange(0, 600), parent, f"title {i}"))
            for k in range(rng.randrange(1, 4)):
                msgs.append((sid, "user" if k == 0 else "assistant", PROMPT if k == 0 else "ok", ts + k))
        conn.executemany("INSERT INTO sessions (id, source, started_at, last_activity_at, message_count,"
                         " parent_session_id, title) VALUES (?, ?, ?, ?, 1, ?, ?)", rows)
        conn.executemany("UPDATE sessions SET end_reason = 'compression', ended_at = started_at + 1 WHERE id = ?",
                         [(r[4],) for r in rows if r[4]])
        conn.executemany("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)", msgs)
    db._execute_write(write)
    return ids


def read(db, **kw):
    return db.list_sessions_rich(order_by_last_active=True, compact_rows=True, **kw)


def _timed(fn) -> float:
    t = time.thread_time()
    fn()
    return (time.thread_time() - t) * 1000


def scale(n: int) -> dict:
    rng = random.Random(n)
    page = SLICES[0]
    with tempfile.TemporaryDirectory() as tmp:
        db = SessionDB(db_path=Path(tmp) / "state.db")
        try:
            ids = seed(db, n, rng)
            recency.clear()
            recency.ENABLED = False
            exact = statistics.median(_timed(lambda: read(db, **page)) for _ in range(5))
            recency.ENABLED = True
            read(db, **page)
            served = statistics.median(_timed(lambda: read(db, **page)) for _ in range(5))
            clock = [BASE + n * 40]

            def heartbeat():
                clock[0] += 1
                db._execute_write(lambda c: c.execute("UPDATE sessions SET last_activity_at = ? WHERE id = ?",
                                                      (clock[0], rng.choice(ids))))
                return _timed(lambda: read(db, **page))

            def message():
                db.append_message(session_id=rng.choice(ids), role="assistant", content="tick")
                return _timed(lambda: read(db, **page))

            hb = statistics.median(heartbeat() for _ in range(9))
            msg = statistics.median(message() for _ in range(9))
            return dict(sessions=n, exact_ms=round(exact, 2), served_ms=round(served, 2),
                        heartbeat_rerank_ms=round(hb, 2), message_rerank_ms=round(msg, 2),
                        outcomes=dict(memo.OUTCOMES))
        finally:
            db.close()
            recency.clear()
            memo.OUTCOMES.clear()


def replay(n: int, rounds: int, store: str | None = None) -> dict:
    out: dict = {}
    for mode in ("exact", "memo"):
        r = random.Random(99)
        with tempfile.TemporaryDirectory() as tmp:  # TMPDIR: never beside the live store
            if store:  # a real store: replay on a consistent private copy (sqlite backup API)
                import sqlite3
                src = sqlite3.connect(f"file:{store}?mode=ro", uri=True)  # health: allow HX015 -- read-only source of a scratch copy for the benchmark
                dst = sqlite3.connect(Path(tmp) / "state.db")  # health: allow HX015 -- private scratch copy, not a live store
                src.backup(dst)
                src.close()
                dst.close()
            db = SessionDB(db_path=Path(tmp) / "state.db")
            real_enabled = memo.ENABLED
            try:
                if store:
                    with db._read_ctx() as conn:
                        ids = [row[0] for row in conn.execute(
                            "SELECT id FROM sessions ORDER BY started_at DESC LIMIT 4000")][::-1]
                    n = 0
                else:
                    ids = seed(db, n, random.Random(1))
                recency.clear()
                memo.OUTCOMES.clear()
                if mode == "exact":
                    memo.ENABLED = False  # today's exact ranking
                spent, clock, per_slice = 0.0, BASE + n * 40, {}
                for _ in range(rounds):
                    for _ in range(r.randrange(0, 6)):  # writes between two Desktop refreshes
                        sid = r.choice(ids[-400:]) if r.random() < 0.9 else r.choice(ids)
                        roll = r.random()
                        clock += r.random() * 3
                        if roll < 0.45:
                            db._execute_write(lambda c: c.execute(
                                "UPDATE sessions SET last_activity_at = ? WHERE id = ?", (clock, sid)))
                        elif roll < 0.85:
                            db.append_message(session_id=sid, role="assistant", content="tick")
                        elif roll < 0.93:
                            db._execute_write(lambda c: c.execute(
                                "UPDATE sessions SET title = ? WHERE id = ?", (f"t{clock}", sid)))
                        elif roll < 0.97:
                            db._execute_write(lambda c: c.execute(
                                "UPDATE sessions SET archived = 1 - archived WHERE id = ?", (sid,)))
                        else:
                            db._execute_write(lambda c: c.execute(
                                "UPDATE messages SET active = 0 WHERE id = (SELECT MAX(id) FROM messages"
                                " WHERE session_id = ?)", (sid,)))
                    for i, sl in enumerate(SLICES):
                        ms = _timed(lambda: read(db, **sl))
                        spent += ms
                        per_slice[i] = per_slice.get(i, 0.0) + ms
                out[mode] = dict(cpu_ms_per_refresh=round(spent / rounds, 2), outcomes=dict(memo.OUTCOMES),
                                 per_slice_ms={f"limit{SLICES[i]['limit']}": round(v / rounds, 2)
                                               for i, v in per_slice.items()})
            finally:
                memo.ENABLED = real_enabled
                db.close()
                recency.clear()
    out["ratio"] = round(out["memo"]["cpu_ms_per_refresh"] / out["exact"]["cpu_ms_per_refresh"], 3)
    total = sum(out["memo"]["outcomes"].values()) or 1
    out["fallback_rate"] = round(sum(v for k, v in out["memo"]["outcomes"].items()
                                     if k.startswith("fallback/")) / total, 3)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="5000,23000")
    ap.add_argument("--rounds", type=int, default=60)
    ap.add_argument("--replay-size", type=int, default=5000)
    ap.add_argument("--store", help="replay on a private copy of this real state.db (skips the scale run)")
    ap.add_argument("--json")
    a = ap.parse_args()
    if a.store:
        result = {"replay": replay(0, a.rounds, a.store)}
        print(json.dumps(result, indent=1))
        if a.json:
            Path(a.json).write_text(json.dumps(result, indent=1), encoding="utf-8")
        return 0
    sizes = [int(s) for s in a.sizes.split(",")]
    result: dict = {"scale": [scale(n) for n in sizes]}
    lo, hi = result["scale"][0], result["scale"][-1]
    result["growth"] = {k: round(hi[k] / lo[k], 3) for k in ("exact_ms", "served_ms", "heartbeat_rerank_ms",
                                                              "message_rerank_ms") if lo[k]}
    result["replay"] = replay(a.replay_size, a.rounds)
    print(json.dumps(result, indent=1))
    if a.json:
        Path(a.json).write_text(json.dumps(result, indent=1), encoding="utf-8")
    worst = max(result["growth"].get(k, 0) for k in ("served_ms", "heartbeat_rerank_ms", "message_rerank_ms"))
    return 0 if worst <= 1.5 else 1


if __name__ == "__main__":
    raise SystemExit(main())
