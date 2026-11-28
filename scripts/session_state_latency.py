#!/usr/bin/env python3
"""SessionState v2 latency harness on a disposable home and board. [daveey]

Measures, with the production worker cadence (``tick_s`` 0.1 s) and the real watchers:

* event -> push: a SEPARATE writer process creates/blocks/unblocks cards linked to one chat on the board; the clock
  starts just before each commit in the writer and stops when the store pushes an object showing that change;
* GET: ``GET /api/session-state/<id>`` through the real router (FastAPI TestClient, in-process ASGI).

Usage: ``bash scripts/run-in-hermes-env python3 scripts/session_state_latency.py [--events 60] [--gets 500]``.
Prints one JSON line with p50/p95/max in milliseconds. Exit 1 when a target (push p95 < 500 ms, GET p95 < 50 ms)
is missed. Nothing outside the disposable directory is touched.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_WRITER = r"""
import json, sys, time
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
conn = connect()
n, gap = int(sys.argv[1]), float(sys.argv[2])
request = {"action": "Approve?", "why_human": "Only David can approve.", "interaction": "approval",
           "scope": "deploy", "evidence": "CI green.", "choices": [{"label": "Yes", "value": "y"},
           {"label": "No", "value": "n"}]}
task = None
for i in range(n):
    step = i % 3
    t0 = time.time()
    if step == 0:
        task = kb.create_task(conn, title=f"probe-{i}", assignee="worker", session_id="chat")
        expect = ["created", task]
    elif step == 1:
        kb.block_task(conn, task, kind="needs_input", human_request=request)
        expect = ["blocked", task]
    else:
        kb.unblock_task(conn, task)
        expect = ["unblocked", task]
    print(json.dumps({"i": i, "t0": t0, "expect": expect}), flush=True)
    time.sleep(gap)
"""


def _pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(p / 100 * len(ordered)) - 1))] if ordered else float("nan")


def _summary(values: list[float]) -> dict:
    return {"n": len(values), "p50_ms": round(_pct(values, 50) * 1000, 2), "p95_ms": round(_pct(values, 95) * 1000, 2),
            "max_ms": round(max(values) * 1000, 2) if values else None}


def _matches(obj: dict, expect: list[str]) -> bool:
    kind, task = expect
    card = (obj.get("cards") or {}).get(f"default/{task}")
    if kind == "created":
        return card is not None
    if kind == "blocked":
        return card is not None and card.get("blocked_on") == "human"
    return card is not None and card.get("lifecycle") != "blocked"


def run(events: int, gets: int, gap: float, root: str | None = None) -> dict:
    home = Path(tempfile.mkdtemp(prefix="session-state-latency-", dir=root or os.environ.get("TMPDIR")))
    for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        os.environ.pop(name, None)
    os.environ.update(HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home),
                      TMPDIR=str(Path(root or tempfile.gettempdir())))
    sys.path.insert(0, str(ROOT))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hermes_cli.kanban_db_connect import connect
    from hermes_cli.web_routers import session_state as routes
    from hermes_session_state_events import install_watchers
    from hermes_session_state_store import SessionStateStore, install
    from hermes_state import SessionDB

    connect().close()  # create the board before the watcher's first look
    db = SessionDB(db_path=home / "state.db")
    db.create_session("chat", source="desktop")
    store = SessionStateStore(db)
    install_watchers(store, state_path=home / "state.db", user_steps_path=None)
    install(store)
    pushes: list[tuple[float, dict]] = []
    lock = threading.Lock()

    def on_push(obj: dict) -> None:
        with lock:
            pushes.append((time.time(), obj))
    store.subscribe(on_push)
    store.recompute("chat")
    store.start(tick_s=0.1)
    time.sleep(0.5)
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.run([sys.executable, "-c", _WRITER, str(events), str(gap)], env=env, cwd=str(ROOT),
                          capture_output=True, text=True, timeout=events * (gap + 5) + 60, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"writer failed: {proc.stderr[-2000:]}")
    time.sleep(2.0)
    writes = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    latencies, missed = [], 0
    with lock:
        seen = list(pushes)
    for write in writes:
        hit = next((at for at, obj in seen if at >= write["t0"] and _matches(obj, write["expect"])), None)
        if hit is None:
            missed += 1
        else:
            latencies.append(hit - write["t0"])
    app = FastAPI()
    app.include_router(routes.router)
    routes._ws_allowed = lambda ws: True
    get_times = []
    with TestClient(app) as client:
        for _ in range(gets):
            t0 = time.perf_counter()
            reply = client.get("/api/session-state/chat")
            get_times.append(time.perf_counter() - t0)
            assert reply.status_code == 200
    cards = len((store.get("chat") or {}).get("cards") or {})
    store.stop()
    install(None)
    db.close()
    return {"event_to_push": _summary(latencies), "missed_events": missed, "get": _summary(get_times),
            "cards_in_object": cards, "metrics": store.metrics(), "home": str(home)}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=60)
    parser.add_argument("--gets", type=int, default=500)
    parser.add_argument("--gap", type=float, default=0.25)
    parser.add_argument("--root", default=None, help="parent dir of the disposable home (default: $TMPDIR)")
    args = parser.parse_args(argv)
    result = run(args.events, args.gets, args.gap, args.root)
    ok = result["missed_events"] == 0 and result["event_to_push"]["p95_ms"] < 500 and result["get"]["p95_ms"] < 50
    result["targets_met"] = ok
    print(json.dumps(result, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
