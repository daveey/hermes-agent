"""Scale + replay benchmark for the annotation holder's narrow origin key (card B, t_01a1d59e531bd0a4).

Run: scripts/run-in-hermes-env python scripts/benchmark_kanban_annotation_holder.py [--wal-mb N] [--json PATH]
     [--union PATH] [--per-refresh-ms MS] [--probe-ms MS] [--tiers small,large]

Two parts, both printed as one JSON document:

1. **Scale** (real code, real CPU). A synthetic hub.db replica per tier (tasks / replica rows / session rows) plus
   an origin ledger. Each simulated 2 s window replays the rf2 change mix (t01a1d563-rf2.json): ~1.9 changed task
   rows (67% liveness-only: ``activity_at``/``activity_kind`` + observation ``task_hash`` restamp), ~7.9 changed
   session rows, then calls the real ``kanban_annotation_narrow.key`` and records its process CPU. ``--wal-mb``
   first grows the WAL to >= N MB with autocheckpoint off (card M: read cost follows the active log), else the
   WAL is checkpointed (TRUNCATE) before the replay.

2. **Replay** (decision model). The C2 census stream (``c2-union.json``: per 2 s window, which components of the
   default scope's union moved) drives the holder rule: 2 s probe, refresh only when the narrow key moved and the
   copy is >= 10 s old, forced renewal at 20 s. Baseline = today's rule (refresh on every exact-fingerprint move,
   65% of 2 s passes in chg1). Holder CPU% = refreshes x per-refresh cost (P1 post-C measurement, inclusive) +
   narrow-key calls x measured key cost + exact probes x probe cost.

Budget (card B): holder <= 2.4% of a core, poll path <= 0.3%. The script reports ``over_budget`` honestly; it does
not shrink the per-refresh cost (that is the measured production number, see comment on the card).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

TIERS = {
    # name: (linked tasks, replica task rows, session rows, ledger obligations)
    "small": (1000, 10_000, 5_000, 200),
    "mid": (2000, 20_000, 12_000, 400),
    "large": (5800, 40_000, 23_000, 800),
}
WINDOWS = 150
ORIGIN = ("me", "default")


STATE: dict[int, dict] = {}


def _task(tid: str, linked: bool, *, status="running", at=1.0, kind="tool", n=0) -> str:
    """A replica task record with a VALID owner observation (schema 1, task_hash over the payload), so an
    activity restamp moves ``task_hash`` exactly as production publication does."""
    from hermes_cli.kanban_origin_ledger import _json

    rec = {"id": tid, "title": f"task {tid}", "status": status, "board": "main", "assignee": "w",
           "body": "x" * 200, "activity_at": at, "activity_kind": kind, "comments": n}
    if linked:
        rec["sessions"] = [{"session": f"s{int(tid[1:]) % 400}", "gw": ORIGIN[0], "profile": ORIGIN[1],
                            "rel": "shepherd"}]
    rec["origin_observation"] = {"schema": 1, "version": 1, "gateway": "owner",
                                 "task_hash": hashlib.sha256(_json(rec).encode()).hexdigest()}
    return json.dumps(rec)


class _Conn(sqlite3.Connection):
    seq = 0


def build_hub(path: Path, linked: int, tasks: int, sessions: int) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, factory=_Conn)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""CREATE TABLE replica(gw TEXT, coll TEXT, key TEXT, data TEXT,
        updated_at REAL, version INTEGER, seq INTEGER, PRIMARY KEY(gw, coll, key));
        CREATE INDEX replica_poll_metadata ON replica(coll, seq);
        CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE filler(id INTEGER PRIMARY KEY, b BLOB);""")
    conn.execute("BEGIN")
    conn.execute("INSERT INTO meta VALUES('gateway', ?)", (ORIGIN[0],))
    conn.execute("INSERT INTO meta VALUES('last_ok', ?)", (str(time.time()),))
    seq = 0
    for i in range(tasks):
        seq += 1
        tid = f"t{i}"
        conn.execute("INSERT INTO replica VALUES('owner','tasks',?,?,1,1,?)",
                     (f"default/main/{tid}", _task(tid, i < linked), seq))
    for i in range(sessions):
        seq += 1
        conn.execute("INSERT INTO replica VALUES('owner','sessions',?,?,1,1,?)",
                     (f"s{i}", json.dumps({"id": f"s{i}", "message_count": 0, "activity": 0}), seq))
    info = {"gateway": "owner", "scope_heartbeat_period_s": 183,
            "task_scopes": [{"board": "main", "observed_at": time.time(), "profile": "default", "state": "ok"}]}
    seq += 1
    conn.execute("INSERT INTO replica VALUES('owner','gateway','info',?,1,1,?)", (json.dumps(info), seq))
    conn.execute("COMMIT")
    conn.seq = seq
    return conn


def build_ledger(path: Path, obligations: int) -> None:
    from hermes_cli import kanban_origin_ledger as O

    c = sqlite3.connect(path, isolation_level=None)
    c.execute("CREATE TABLE origin_obligations (binding_id TEXT PRIMARY KEY, binding_hash TEXT NOT NULL,"
              "binding_json TEXT NOT NULL, resolved_session TEXT NOT NULL, task_json TEXT NOT NULL,"
              "observation_version INTEGER NOT NULL, released INTEGER NOT NULL DEFAULT 0)")
    c.execute("CREATE TABLE origin_receipts (request_id TEXT PRIMARY KEY NOT NULL, binding_hash TEXT NOT NULL,"
              "receipt_json TEXT NOT NULL)")
    c.execute("BEGIN")
    for i in range(obligations):
        b = {"binding_id": f"b{i}", "version": 1,
             "owner": {"gateway": "owner", "profile": "default", "board": "main", "task_id": f"t{i}"},
             "origin": {"gateway": ORIGIN[0], "profile": ORIGIN[1], "session": f"s{i % 400}"},
             "task_record_key": f"default/main/t{i}", "publication_version": 1}
        h = O.binding_hash(b)
        c.execute("INSERT INTO origin_obligations VALUES (?,?,?,?,?,?,?)",
                  (O._binding_key(b), h, O._json(b), "res", json.dumps({"id": f"t{i}"}), 1, 0))
        c.execute("INSERT INTO origin_receipts VALUES (?,?,?)",
                  (f"r{i}", h, json.dumps({"binding_hash": h, "status": "accepted"})))
    c.execute("COMMIT")
    c.close()


def grow_wal(conn: sqlite3.Connection, path: Path, mb: int) -> int:
    conn.execute("PRAGMA wal_autocheckpoint=0")
    blob = os.urandom(1 << 20)
    i = 0
    while os.path.getsize(str(path) + "-wal") < mb << 20:
        conn.execute("INSERT OR REPLACE INTO filler VALUES(?, ?)", (i % 64, blob))
        i += 1
    return os.path.getsize(str(path) + "-wal")


def churn(conn: sqlite3.Connection, rng: random.Random, linked: int, tasks: int, sessions: int, step: int) -> dict:
    """One 2 s window of the rf2 mix. Returns what kind of change happened to linked rows."""
    kinds = {"liveness_linked": 0, "substantive_linked": 0, "unlinked": 0, "sessions": 0}
    conn.execute("BEGIN")
    n_tasks = 2 if rng.random() < 0.9 else 1  # rf2: 284 changed task rows / 150 windows
    for _ in range(n_tasks):
        conn.seq += 1
        i = rng.randrange(tasks) if rng.random() < 0.5 else rng.randrange(linked)
        liveness = rng.random() < 0.67  # rf2: 191/284 activity-only
        st = STATE.setdefault(i, {"status": "running", "n": 0, "at": 1.0, "kind": "tool"})
        if liveness:  # only the liveness fields move; substantive fields keep their current value
            st.update(at=1000.0 + step, kind=rng.choice(["tool", "reply"]))
        else:
            st.update(status=rng.choice(["running", "blocked", "done"]), n=step + 1)
        data = _task(f"t{i}", i < linked, **st)
        conn.execute("UPDATE replica SET data=?, seq=?, version=version+1 WHERE gw='owner' AND coll='tasks' AND key=?",
                     (data, conn.seq, f"default/main/t{i}"))
        if i < linked:
            kinds["liveness_linked" if liveness else "substantive_linked"] += 1
        else:
            kinds["unlinked"] += 1
    for _ in range(8):  # rf2: 1185 session rows / 150 windows
        conn.seq += 1
        s = rng.randrange(sessions)
        conn.execute("UPDATE replica SET data=?, seq=? WHERE gw='owner' AND coll='sessions' AND key=?",
                     (json.dumps({"id": f"s{s}", "message_count": step, "activity": step}), conn.seq, f"s{s}"))
        kinds["sessions"] += 1
    conn.execute("COMMIT")
    return kinds


def scale_tier(name: str, wal_mb: int, seed: int = 7) -> dict:
    from hermes_cli import kanban_annotation_narrow as narrow
    from hermes_cli import kanban_session_activity as ksa

    linked, tasks, sessions, obligations = TIERS[name]
    STATE.clear()
    tmp = Path(tempfile.mkdtemp(prefix=f"ann-holder-{name}-", dir=os.environ.get("TMPDIR")))
    hub, ledger = tmp / "hub.db", tmp / "origin-ledger.db"
    conn = build_hub(hub, linked, tasks, sessions)
    build_ledger(ledger, obligations)
    if wal_mb:
        wal = grow_wal(conn, hub, wal_mb)
    else:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        wal = os.path.getsize(str(hub) + "-wal")
    ksa._hub_db_path = lambda: hub
    narrow._ledger_path = lambda: ledger
    narrow.reset()
    scope = ("home", ORIGIN[1], "kh", "hub", "mail")
    full = ("hubsig", ("board", 1))
    t0 = time.process_time()
    first = narrow.key(scope, full, [])
    cold_ms = (time.process_time() - t0) * 1000
    assert first is not None, "narrow key unavailable on a healthy store"
    rng = random.Random(seed)
    costs, moved, liveness_only_windows, liveness_moved = [], 0, 0, 0
    previous = first
    for step in range(WINDOWS):
        kinds = churn(conn, rng, linked, tasks, sessions, step)
        t0 = time.process_time()
        k = narrow.key(scope, full, [])
        costs.append((time.process_time() - t0) * 1000)
        assert k is not None, f"narrow key unavailable at step {step}"
        if k != previous:
            moved += 1
        if kinds["substantive_linked"] == 0:
            liveness_only_windows += 1
            liveness_moved += k != previous
        previous = k
    conn.close()
    costs.sort()
    return {"tier": name, "linked": linked, "replica_tasks": tasks, "sessions": sessions,
            "obligations": obligations, "wal_bytes": wal, "cold_ms": round(cold_ms, 3),
            "key_ms_mean": round(statistics.fmean(costs), 3), "key_ms_p50": round(costs[len(costs) // 2], 3),
            "key_ms_p95": round(costs[int(len(costs) * 0.95)], 3), "key_ms_max": round(costs[-1], 3),
            "windows": WINDOWS, "key_moved_windows": moved, "windows_without_substantive_linked": liveness_only_windows,
            "key_moved_without_substantive_linked": liveness_moved, "narrow_stats": dict(narrow.stats)}


def replay(union: dict, key_ms: float, per_refresh_ms: float, probe_ms: float, exact_move_rate: float) -> dict:
    """Holder decisions over the c2 census stream for the default scope."""
    comps = {"hub", "ledger", "own_boards", "shadows", "freshness_bucket"}
    samples = union["samples"]
    last, refreshes, renews, keys, probes, pending = -1e9, 0, 0, 0, 0, False
    exact_since = False
    for i, s in enumerate(samples):
        t = (i + 1) * 2.0
        moved = set(s["moved"].get("default", ()))
        probes += 1
        exact_since = exact_since or bool(moved) or random.random() < exact_move_rate
        pending = pending or bool(moved & comps)
        if t - last >= 20.0:
            refreshes, renews, last, pending, exact_since = refreshes + 1, renews + 1, t, False, False
            continue
        if t - last < 10.0 or not exact_since:
            continue
        keys += 1  # exact moved past the floor: the holder computes the narrow key
        if pending:
            refreshes, last, pending, exact_since = refreshes + 1, t, False, False
    elapsed = len(samples) * 2.0
    holder = (refreshes * per_refresh_ms + keys * key_ms + probes * probe_ms) / 1000 / elapsed * 100
    base_refreshes = sum(1 for _ in samples if random.random() < exact_move_rate)
    base = (base_refreshes * per_refresh_ms + len(samples) * probe_ms) / 1000 / elapsed * 100
    renew_floor = (elapsed / 20.0) * per_refresh_ms / 1000 / elapsed * 100
    return {"elapsed_s": elapsed, "refreshes": refreshes, "renewals": renews, "narrow_key_calls": keys,
            "probes": probes, "refresh_per_s": round(refreshes / elapsed, 4), "holder_pct": round(holder, 4),
            "baseline_refreshes": base_refreshes, "baseline_pct": round(base, 4),
            "renewal_only_floor_pct": round(renew_floor, 4), "narrow_key_pct": round(keys * key_ms / 1000 / elapsed * 100, 4),
            "budget_pct": 2.4, "over_budget": holder > 2.4}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wal-mb", type=int, default=0)
    ap.add_argument("--tiers", default="small,mid,large")
    ap.add_argument("--union", default="")
    ap.add_argument("--per-refresh-ms", type=float, default=553.605)  # P1 post-C, default scope, inclusive
    ap.add_argument("--probe-ms", type=float, default=8.585)  # P1 post-C waiting input probe
    ap.add_argument("--exact-move-rate", type=float, default=0.65)  # chg1: exact fingerprint moves per 2 s pass
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="ann-holder-home-", dir=os.environ.get("TMPDIR")))
    os.environ["HERMES_HOME"] = str(tmp)
    os.environ.setdefault("HERMES_ALLOW_KANBAN_TEST_DB", "1")
    random.seed(11)
    out: dict = {"wal_mb_requested": args.wal_mb, "scale": [scale_tier(t, args.wal_mb) for t in args.tiers.split(",")]}
    worst_key_ms = max(s["key_ms_mean"] for s in out["scale"])
    if args.union:
        out["replay"] = replay(json.loads(Path(args.union).read_text(encoding="utf-8-sig")), worst_key_ms, args.per_refresh_ms,
                               args.probe_ms, args.exact_move_rate)
    text = json.dumps(out, indent=1)
    print(text)
    if args.json:
        Path(args.json).write_text(text, encoding="utf-8")
    # Hard gates for this card's own work (the narrow key): liveness-only churn must never move it, and its
    # holder-side share over the replay (calls x the largest tier's mean cost) must stay <= 0.5% of a core.
    # The total holder figure is REPORTED (``over_budget``), not gated here: the forced 20 s renewal alone costs
    # ``renewal_only_floor_pct`` at the measured per-refresh cost, so 2.4% needs per-refresh work cuts (P1).
    bad = [s["tier"] for s in out["scale"] if s["key_moved_without_substantive_linked"]]
    if out.get("replay") and out["replay"]["narrow_key_pct"] > 0.5:
        bad.append("narrow_key_pct")
    if bad:
        print("GATE FAILED:", bad, file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
