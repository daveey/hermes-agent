"""Unstubbed local supervisor-drain replay, including its idle executor clock. [daveey]

Run with scripts/run-in-hermes-env python scripts/kanban_supervisor_replay_bench.py
--ticks 240 --output <json>. Synthetic empty boards; not a hub P1 budget proof.
Neither drain nor its poll/apply_incoming calls are replaced. No gateway is configured,
so replica-side operator delivery is not represented. Network/process writes are refused.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


def _refuse(*_args, **_kwargs):
    raise AssertionError("supervisor replay must not access network or spawn processes")


def replay(conn, ticks: int) -> dict:
    from hermes_cli import kanban_supervisor_requests as sr
    from hermes_cli import kanban_sweep_gate as gate

    gate.reset()
    start = int(time.time())
    result = {}
    for name, run in (("ungated", sr.drain), ("gated", sr.dispatcher_drain)):
        gate.reset()
        # Reset the executor clock to the beginning of this identical simulated timeline.
        conn.execute("DELETE FROM supervisor_executors")
        conn.commit()
        statements = []
        conn.set_trace_callback(statements.append)
        cpu = time.thread_time()
        with patch.object(socket.socket, "connect", _refuse), patch.object(subprocess, "Popen", _refuse):
            for tick in range(ticks):
                with patch.object(sr.time, "time", return_value=start + 15 * tick):
                    run(conn)
        elapsed = time.thread_time() - cpu
        conn.set_trace_callback(None)
        result[name] = {
            "ticks": ticks, "cpu_ms_per_tick": 1000 * elapsed / ticks,
            "core_pct_at_15s": 100 * elapsed / (15 * ticks),
            "sql_statements": len(statements),
            "executor_upserts": sum("INSERT INTO supervisor_executors" in q for q in statements),
            "last_seen": conn.execute("SELECT MAX(last_seen) FROM supervisor_executors").fetchone()[0],
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticks", type=int, default=240)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.ticks <= 0:
        parser.error("--ticks must be positive")
    with tempfile.TemporaryDirectory(prefix="supervisor-replay-") as home:
        os.environ["HERMES_HOME"] = home
        for key in tuple(os.environ):
            if key.startswith(("HERMES_KANBAN", "HERMES_SESSION")):
                del os.environ[key]
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc
        from hermes_cli import kanban_supervisor_requests as sr
        kb.init_db()
        with kbc.connect_closing() as conn:
            sr.ensure_schema(conn)
            conn.commit()
            result = replay(conn, args.ticks)
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
