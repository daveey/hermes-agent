#!/usr/bin/env python3
"""Write the wire ops core's voluntary endings send with a ``source_receipt`` (workspace_source v1, plan v4.1 §A2).

Each case runs one real ending (``complete_task``, ``schedule_task``, ``block_task``) on a claimed card with a
staged receipt, then records what ``push`` would send for that card: ``translate(_full_row_if_unseen(conn, row))``
for every pending outbox op, in order. hermes-hub commits the output as
``worker/test/fixtures/source_receipt_ops.json`` and its vitest ``receipt_fixture_from_core`` replays the ops
through the real Board ``rows.ts``/``transitions.ts``.

Run (temp HERMES_HOME; never touches ~/.hermes):

    scripts/run-in-hermes-env python scripts/gen_source_receipt_fixture.py OUT.json
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

for _k in [k for k in os.environ if k.startswith("HERMES_")]:  # a worker/delegated parent fences new boards
    del os.environ[_k]
_TMP = Path(tempfile.mkdtemp(prefix="source-receipt-fixture-"))
os.environ["HERMES_HOME"] = str(_TMP / ".hermes")
os.environ["HOME"] = str(_TMP)
Path(os.environ["HERMES_HOME"]).mkdir(parents=True)
(Path(os.environ["HERMES_HOME"]) / "config.yaml").write_text("kanban:\n  sync:\n    enabled: true\n", encoding="utf-8")
Path.home = classmethod(lambda cls: _TMP)  # type: ignore[assignment]

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_source_receipt as ksr
from hermes_cli import kanban_sync_client as ksc

COMMIT = "a" * 40


def _applying(conn, sql, params=()):
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET applying = 1")
        conn.execute(sql, params)
        conn.execute("UPDATE kanban_sync_state SET applying = 0")


def _running(conn, title, acked):
    tid = kb.create_task(conn, title=title, assignee="worker")
    assert kb.claim_task(conn, tid, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
    if acked:
        _applying(conn, "UPDATE tasks SET sync_version = 1 WHERE id = ?", (tid,))
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox")
    run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    run_uid = conn.execute("SELECT uid FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0]
    return tid, run_id, run_uid


def _wire(conn, tid):
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM kanban_sync_outbox WHERE table_name = 'tasks' AND row_uid = ? AND state = 'pending' "
        "ORDER BY created_at, rowid", (tid,))]
    from hermes_cli import kanban_sync_wire as wire_ops
    return [wire_ops.translate(ksc._full_row_if_unseen(conn, r), "hlc") for r in rows]


ENDINGS = {
    "complete": (lambda c, t, r: kb.complete_task(c, t, result="ok", summary="done", expected_run_id=r), "done"),
    "schedule": (lambda c, t, r: kb.schedule_task(c, t, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z",
                                                  clock_only=True, expected_run_id=r,
                                                  awaiting={"kind": "clock", "why": "fixture release slot"}),
                 "scheduled"),
    "block": (lambda c, t, r: kb.block_task(c, t, reason="1) tap Approve", kind="needs_input",
                                            expected_run_id=r), "blocked"),
}


def main(out: str) -> None:
    conn = kbc.connect(Path(os.environ["HERMES_HOME"]) / "kanban.db")
    cases = []
    for name, acked in [("complete", True), ("schedule", True), ("block", True), ("complete", False)]:
        end, status = ENDINGS[name]
        tid, run_id, run_uid = _running(conn, f"fixture {name}", acked)
        receipt = {"run_uid": run_uid, "state": "unchanged", "manifest_revision": 1, "commit": COMMIT}
        with ksr.staged(tid, run_id, receipt):
            assert end(conn, tid, run_id)
        ops = _wire(conn, tid)
        assert sum("source_receipt" in op["fields"] for op in ops) == 1, ops
        cases.append({"ending": name, "acked": acked, "status": status, "task": tid, "run_uid": run_uid,
                      "receipt": receipt, "ops": ops})
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, encoding="utf-8", timeout=30,
                          cwd=Path(__file__).resolve().parents[1]).stdout.strip()
    Path(out).write_text(json.dumps({"generator": "scripts/gen_source_receipt_fixture.py", "core_commit": head,
                                     "cases": cases}, indent=1, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1])
