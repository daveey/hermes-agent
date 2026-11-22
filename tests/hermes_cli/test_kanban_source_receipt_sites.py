"""workspace_source v1 core PR-1, plan v4.1 §A5 + build decisions Q2/Q4 (t_01a1d0ec4ab93f32).

Every voluntary ending wraps its one status-leaving UPDATE in ``with_source_receipt`` so a staged receipt rides
that op (and only that op); ``kanban_awaiting.escalate`` reuses the receipt its enclosing ending staged. The AST
guard classifies every status-leaving ``UPDATE tasks`` in ``hermes_cli/`` and ``plugins/`` as wrapped or
involuntary, so a new ending can never ship unclassified.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def conn(kanban_home):
    yield from support.synced_conn(kanban_home)


def _ksr():
    from hermes_cli import kanban_source_receipt as ksr
    return ksr


def _receipt(run_uid: str) -> dict:
    return {"run_uid": run_uid, "state": "unchanged", "manifest_revision": 1, "commit": "b" * 40}


def _applying(conn, sql, params=()):
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET applying = 1")
        conn.execute(sql, params)
        conn.execute("UPDATE kanban_sync_state SET applying = 0")


def _run_of(conn, tid):
    run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    return run_id, conn.execute("SELECT uid FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0]


def _clear_outbox(conn):
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox")


def _running(conn, title="sites"):
    tid = kb.create_task(conn, title=title, assignee="worker")
    assert kb.claim_task(conn, tid, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
    _applying(conn, "UPDATE tasks SET sync_version = 1 WHERE id = ?", (tid,))
    _clear_outbox(conn)
    return (tid, *_run_of(conn, tid))


def _reviewing(conn):
    """A card a reviewer claimed from ``review``: its running run is the reviewer's."""
    tid, run_id, _uid = _running(conn, "review me")
    assert kb.request_review(conn, tid, summary="ready for review", expected_run_id=run_id)
    assert kb.claim_review_task(conn, tid, ttl_seconds=900, claimer=f"{kb._host_prefix()}:2")
    _clear_outbox(conn)
    return (tid, *_run_of(conn, tid))


def _wire(conn, tid):
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM kanban_sync_outbox WHERE table_name = 'tasks' AND row_uid = ? AND state = 'pending' "
        "ORDER BY created_at, rowid", (tid,))]
    from hermes_cli import kanban_sync_wire as wire_ops
    return [wire_ops.translate(ksc._full_row_if_unseen(conn, r), "hlc") for r in rows]


def _pending(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM kanban_source_receipt_pending").fetchone()[0]


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0]


def _continue(conn, tid, run_id):
    from hermes_cli.kanban_continuation import continue_task
    return continue_task(conn, tid, checkpoint="halfway", expected_run_id=run_id)["ok"]


def _timed_park(conn, tid, run_id):
    return kb.block_task(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z", kind="time", clock_only=True, awaiting={"kind": "clock", "why": "fixture window"},
                         expected_run_id=run_id)


def _request_review(conn, tid, run_id):
    return kb.request_review(conn, tid, summary="please review", expected_run_id=run_id)


def _request_changes(conn, tid, run_id):
    return kb.request_changes(conn, tid, reason="fix the nit", expected_run_id=run_id)[0]


def _escalate_schedule(conn, tid, run_id, monkeypatch):
    """A third park on one premise: ``schedule_task`` escalates (``kanban_awaiting.escalate``) instead of parking."""
    from hermes_cli import kanban_awaiting as kaw

    def repeated(*_a, **_k):
        raise kaw.RepeatedPark({"kind": "clock", "premise": "p1"})
    monkeypatch.setattr(kaw, "gate", repeated)
    return kb.schedule_task(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z", clock_only=True, awaiting={"kind": "clock", "why": "fixture window"},
                            expected_run_id=run_id)


ENDINGS = {
    "continue_task": (_continue, ("ready", "scheduled", "todo")),
    "timed_park": (_timed_park, ("scheduled",)),
    "request_review": (_request_review, ("review",)),
}


def _assert_rides(conn, tid, run_uid, statuses):
    wire = _wire(conn, tid)
    leaving = [i for i, op in enumerate(wire) if op["fields"].get("status") in statuses]
    assert len(leaving) == 1, wire
    assert wire[leaving[0]]["fields"].get("source_receipt") == _receipt(run_uid), wire
    assert not any("source_receipt" in o["fields"] for i, o in enumerate(wire) if i != leaving[0]), wire
    assert _pending(conn) == 0


@pytest.mark.parametrize("ending", sorted(ENDINGS))
def test_receipt_rides_status_leaving_op(conn, ending):
    end, statuses = ENDINGS[ending]
    tid, run_id, run_uid = _running(conn)
    with _ksr().staged(tid, run_id, _receipt(run_uid)):
        assert end(conn, tid, run_id)
    _assert_rides(conn, tid, run_uid, statuses)


def test_request_changes_receipt_rides_status_leaving_op(conn):
    tid, run_id, run_uid = _reviewing(conn)
    with _ksr().staged(tid, run_id, _receipt(run_uid)):
        assert _request_changes(conn, tid, run_id)
    _assert_rides(conn, tid, run_uid, ("ready", "todo"))


def test_escalate_reuses_enclosing_receipt(conn, monkeypatch):
    """Q2: the escalation's own ``running -> todo`` UPDATE carries the receipt the wrapped ending staged."""
    tid, run_id, run_uid = _running(conn)
    with _ksr().staged(tid, run_id, _receipt(run_uid)):
        assert _escalate_schedule(conn, tid, run_id, monkeypatch)
    assert _status(conn, tid) == "todo"
    _assert_rides(conn, tid, run_uid, ("todo",))


def test_close_dead_run_wrapped(conn):
    """``kanban_run_finalizer.close_dead_run`` (A5): a staged receipt for that run rides its UPDATE."""
    from types import SimpleNamespace
    from hermes_cli.kanban_run_finalizer import close_dead_run
    tid, run_id, run_uid = _running(conn)
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    dead = SimpleNamespace(event_payload={}, run_outcome="crashed", error_text="gone", event_kind="crashed")
    with _ksr().staged(tid, run_id, _receipt(run_uid)), kbc.write_txn(conn):
        assert close_dead_run(conn, row, dead) is not None
    _assert_rides(conn, tid, run_uid, ("ready", "todo", "blocked"))


def _race_status(conn, tid, status):
    """A concurrent writer moved the card (applied as a feed row: the capture triggers do not see it)."""
    conn.execute("UPDATE kanban_sync_state SET applying = 1")
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
    conn.execute("UPDATE kanban_sync_state SET applying = 0")


def _refuse_request_changes(conn, tid, run_id, monkeypatch):
    real = kb._landing_status_after_parents

    def racing(c, task_id):
        _race_status(c, task_id, "blocked")
        return real(c, task_id)
    monkeypatch.setattr(kb, "_landing_status_after_parents", racing)
    ok = _request_changes(conn, tid, run_id)
    monkeypatch.setattr(kb, "_landing_status_after_parents", real)
    return ok


def _refuse_continue(conn, tid, run_id, monkeypatch):
    from hermes_cli import kanban_wake_check as kwc
    real = kwc.validate_park

    def racing(*a, **k):
        _race_status(conn, tid, "blocked")
        return real(*a, **k)
    monkeypatch.setattr(kwc, "validate_park", racing)
    ok = _continue(conn, tid, run_id)
    monkeypatch.setattr(kwc, "validate_park", real)
    return ok


def _refuse_block(conn, tid, run_id, monkeypatch):
    from hermes_cli import kanban_dependency_validation as kdv
    real = kdv.validate_dependency_block

    def racing(c, task_id, kind):
        _race_status(c, task_id, "blocked")
        return real(c, task_id, kind)
    monkeypatch.setattr(kdv, "validate_dependency_block", racing)
    ok = kb.block_task(conn, tid, reason="1) tap Approve", kind="needs_input", expected_run_id=run_id)
    monkeypatch.setattr(kdv, "validate_dependency_block", real)
    return ok


REFUSALS = {
    "request_changes_state_changed": (_refuse_request_changes, _reviewing, _request_changes, ("ready", "todo")),
    "continue_concurrent": (_refuse_continue, _running, _continue, ("ready", "scheduled", "todo")),
    "block_refused": (_refuse_block, _running,
                      lambda c, t, r: kb.block_task(c, t, reason="1) tap Approve", kind="needs_input",
                                                    expected_run_id=r), ("blocked",)),
}


@pytest.mark.parametrize("refusal", sorted(REFUSALS))
def test_refused_ending_leaves_no_pending_receipt(conn, monkeypatch, refusal):
    """A refused ending leaves no pending row; the same run's next (accepted) ending still carries its receipt."""
    refuse, setup, retry, statuses = REFUSALS[refusal]
    tid, run_id, run_uid = setup(conn)
    with _ksr().staged(tid, run_id, _receipt(run_uid)):
        assert not refuse(conn, tid, run_id, monkeypatch)
        assert _pending(conn) == 0
        _race_status(conn, tid, "running")  # the racing writer backs out
        _clear_outbox(conn)
        assert retry(conn, tid, run_id)
    _assert_rides(conn, tid, run_uid, statuses)


# --- Q4 ---------------------------------------------------------------------------------------------------------

def test_install_triggers_ensures_pending_table(conn):
    """The tasks UPDATE trigger reads the pending table: (re)installing triggers must create it first."""
    with kbc.write_txn(conn):
        conn.execute("DROP TABLE kanban_source_receipt_pending")
        sync.drop_triggers(conn)
        sync.install_triggers(conn)
    tid = kb.create_task(conn, title="after reinstall", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET priority = 3 WHERE id = ?", (tid,))  # fires the UPDATE capture trigger


def test_old_board_multi_field_refusal_strips_virtual(conn):
    """A two-field ``forbidden_field`` detail naming ``source_receipt``: the virtual field is dropped (no omission)
    and the op is resent without it, so the remaining real field takes the ordinary single-field path."""
    from hermes_cli import kanban_sync_compat as compat
    tid, run_id, run_uid = _running(conn)
    with _ksr().staged(tid, run_id, _receipt(run_uid)):
        assert kb.complete_task(conn, tid, result="ok", summary="done", expected_run_id=run_id)
    row = dict(conn.execute("SELECT * FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid=? "
                            "AND fields LIKE '%source_receipt%'", (tid,)).fetchone())
    from hermes_cli import kanban_sync_wire as wire_ops
    wire = wire_ops.translate(ksc._full_row_if_unseen(conn, row), "hlc")
    detail = "not writable on tasks: result, source_receipt"
    with kbc.write_txn(conn):
        out = compat.enqueue_retry(conn, row, wire, {"reason": "forbidden_field", "detail": detail})
    assert out == compat.VIRTUAL_DROPPED
    assert conn.execute("SELECT COUNT(*) FROM kanban_sync_omissions").fetchone()[0] == 0
    assert conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE op_id = ?", (row["op_id"],)).fetchone() is None
    resent = [json.loads(r[0]) for r in conn.execute(
        "SELECT fields FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid=? AND op=?", (tid, row["op"]))]
    assert any(f.get("status") == "done" and "source_receipt" not in f for f in resent), resent


# --- AST guard (A5 + Q2) ----------------------------------------------------------------------------------------


def test_preclaim_refusal_cannot_end_unclaimed_running_source(conn):
    """Even a stale unclaimed running row is not a preclaim review/ready card."""
    from types import SimpleNamespace
    from hermes_cli.kanban_review_admission import record_refusal
    from hermes_cli.kanban_review_model import ReviewModelsExhausted

    tid, _run_id, _run_uid = _running(conn)
    _applying(conn, "UPDATE tasks SET claim_lock=NULL WHERE id=?", (tid,))
    before = tuple(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    events = [tuple(row) for row in conn.execute("SELECT * FROM task_events WHERE task_id=?", (tid,))]
    runs = [tuple(row) for row in conn.execute("SELECT * FROM task_runs WHERE task_id=?", (tid,))]
    result = SimpleNamespace(auto_blocked=[])
    record_refusal(conn, tid, ReviewModelsExhausted('no independent reviewer'), result, lane='running')
    assert tuple(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == before
    assert [tuple(row) for row in conn.execute("SELECT * FROM task_events WHERE task_id=?", (tid,))] == events
    assert [tuple(row) for row in conn.execute("SELECT * FROM task_runs WHERE task_id=?", (tid,))] == runs
    assert result.auto_blocked == []


_UPDATE = re.compile(r"UPDATE\s+tasks\s+SET\b(?P<set>.*?)(?:\bWHERE\b(?P<where>.*))?$", re.IGNORECASE | re.DOTALL)
_SETS_STATUS = re.compile(r"(?<![\w.])status\s*=\s*(?P<value>'[^']*'|\?|\{\})", re.IGNORECASE)
_STATUS_EQ = re.compile(r"(?<![\w.])status\s*=\s*'(\w+)'", re.IGNORECASE)
_STATUS_IN = re.compile(r"(?<![\w.])status\s+IN\s*\(([^)]*)\)", re.IGNORECASE)

# Every status-leaving ``UPDATE tasks`` that can move a card out of ``running``, by ``path::function``.
WRAPPED = {
    # Stop-only reclaim still preserves staged source custody with the real wrapper.
    "hermes_cli/kanban_refusal_receipt.py::stop_failed_refusal",
    "hermes_cli/kanban_retain.py::_local",  # voluntary own-run stop, real source wrapper
    "hermes_cli/kanban_db.py::complete_task",
    "hermes_cli/kanban_dependency_review.py::apply_block_update",  # block_task
    "hermes_cli/kanban_timed_park.py::park",
    "hermes_cli/kanban_db.py::request_review",
    "hermes_cli/kanban_db.py::request_changes",
    "hermes_cli/kanban_db_schedule.py::schedule_task",
    "hermes_cli/kanban_continuation.py::continue_task",
    "hermes_cli/kanban_run_finalizer.py::close_dead_run",
    "hermes_cli/kanban_awaiting.py::escalate",  # Q2: reuses the enclosing ending's staged receipt
}
INVOLUNTARY = {  # no receipt: custody becomes ``retained`` (fail closed)
    # Containment is a retained-source stop, not completion/publication. The
    # local-only path never syncs a portable source claim; replica restoration
    # projects canonical containment and must not manufacture a publish receipt.
    "hermes_cli/kanban_containment_lifecycle.py::contain_local",
    "hermes_cli/kanban_containment_replica.py::restore_page",
    "hermes_cli/kanban_failure_accounting.py::_record_task_failure",
    "hermes_cli/kanban_manual_reclaim.py::_reclaim",
    "hermes_cli/kanban_db.py::release_stale_claims",
    "hermes_cli/kanban_db.py::invalidate_descendants_for_parent_reopen",
    "hermes_cli/kanban_db_dispatch.py::enforce_max_runtime",
    "hermes_cli/kanban_db_dispatch.py::detect_stale_running",
    "hermes_cli/kanban_db_dispatch.py::reconcile_orphaned_running",
    "hermes_cli/kanban_db_archive.py::archive_task",
    "hermes_cli/kanban_sync_divergence.py::_heal_one",
    "hermes_cli/kanban_transfer.py::_scrub_local_state",
    "hermes_cli/kanban_transfer.py::_relocate_imported_rows",  # export relocation, same scrub
    "plugins/kanban/dashboard/plugin_api.py::_set_status_direct",
}
# Sites the scan cannot prove off-``running`` from SQL text alone, each checked by hand.
NOT_FROM_RUNNING = {
    "hermes_cli/kanban_review_admission.py::record_refusal": "only ready/review lanes; writer transaction requires matching unclaimed row",
    "hermes_cli/kanban_containment_lifecycle.py::resume_local": "requires unclaimed triage under the writer transaction",
    "hermes_cli/kanban_containment_replica.py::_restore_released": "only restores unclaimed non-human triage projections",
    "hermes_cli/kanban_assignment_routing.py::route": "runs inside block_task after apply_block_update left running",
    "hermes_cli/kanban_db.py::recompute_ready": "re-promotes todo/blocked/scheduled cards only",
    "hermes_cli/kanban_terminal_guard.py::enforce_after_apply": "puts a held card back to done/archived; skips running",
    "hermes_cli/kanban_terminal_guard.py::_restore_held": "only moves a done/archived row back to its held status",
}


def _text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):  # f-string SQL: placeholders read as ``{}``
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return (_text(node.left) or "") + (_text(node.right) or "")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return _text(node.func.value)  # ``"...".format(...)``
    return None


def _leaves_running(sql: str) -> bool:
    m = _UPDATE.search(sql)
    if not m or not (setm := _SETS_STATUS.search(m.group("set") or "")):
        return False
    if setm.group("value").strip("'").lower() == "running":
        return False  # enters running (a claim)
    where = m.group("where") or ""
    pinned = [s.lower() for s in _STATUS_EQ.findall(where)]
    for group in _STATUS_IN.findall(where):
        pinned += [s.strip().strip("'").lower() for s in group.split(",")]
    return not pinned or "running" in pinned or any(p in ("?", "{}") or not p.isidentifier() for p in pinned)


def _sites() -> dict[str, set[int]]:
    found: dict[str, set[int]] = {}
    for base in ("hermes_cli", "plugins"):
        for path in sorted((REPO / base).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            rel = path.relative_to(REPO).as_posix()
            owner: dict[ast.AST, str] = {}  # node -> innermost enclosing function name
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for node in ast.walk(fn):  # outer functions are walked first; inner ones overwrite
                        owner[node] = fn.name
            inner = {c for n in ast.walk(tree) if isinstance(n, ast.BinOp) for c in (n.left, n.right)}
            for node, name in owner.items():
                sql = None if node in inner else _text(node)  # a concatenation is read whole, never in parts
                if sql and _leaves_running(sql):
                    found.setdefault(f"{rel}::{name}", set()).add(node.lineno)
    return found


def _calls_wrapper(key: str) -> bool:
    rel, name = key.split("::")
    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    parents = {child: parent for parent in ast.walk(fn) for child in ast.iter_child_nodes(parent)}
    def sql_text(node, before):
        if isinstance(node, ast.Name):
            prior = [(n.lineno, n.value) for n in ast.walk(fn) if isinstance(n, ast.Assign) and n.lineno < before
                     and any(isinstance(t, ast.Name) and t.id == node.id for t in n.targets)]
            line, value = max(prior, key=lambda pair: pair[0], default=(0, None))
            return sql_text(value, line) if value is not None else None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "replace":
            return sql_text(node.func.value, before)
        return _text(node)
    updates = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or getattr(node.func, "attr", None) != "execute" or not node.args:
            continue
        arg = node.args[0]
        sql = sql_text(arg, node.lineno)
        if sql and _leaves_running(sql):
            updates.append(node)
    for update in updates:
        current, wrapped = update, False
        while current in parents:
            parent = parents[current]
            if (isinstance(parent, ast.Call) and len(parent.args) >= 3 and parent.args[2] is current
                    and isinstance(current, ast.Lambda) and
                    (getattr(parent.func, "attr", None) or getattr(parent.func, "id", None)) == "with_source_receipt"):
                wrapped = True
                break
            current = parent
        if not wrapped:
            return False
    return bool(updates)


def test_status_leaving_sites_enumerated():
    sites = _sites()
    known = WRAPPED | INVOLUNTARY | set(NOT_FROM_RUNNING)
    unclassified = sorted(set(sites) - known)
    assert not unclassified, (
        f"new status-leaving UPDATE(s) {unclassified}: wrap the ending in kanban_source_receipt.with_source_receipt "
        "(voluntary) or list it as INVOLUNTARY here (its card's custody then becomes retained)")
    # Nested functions share a name with their enclosing walk; every listed site must still exist.
    assert not sorted(known - set(sites)), f"stale entries: {sorted(known - set(sites))}"
    unwrapped = sorted(k for k in WRAPPED if not _calls_wrapper(k))
    assert not unwrapped, f"voluntary endings without with_source_receipt: {unwrapped}"


@pytest.mark.parametrize("body", [
    'with_source_receipt(conn, tid, lambda: conn.execute("SELECT 1"))\n    conn.execute(sql)',
    'with_source_receipt(conn, tid, lambda: conn.execute(sql))\n    conn.execute(sql)',
    'with_source_receipt(conn, tid, lambda: conn.execute("SELECT 1"))\n    def nested():\n        conn.execute(sql)',
])
def test_guard_rejects_unrelated_wrapper(tmp_path, monkeypatch, body):
    import sys
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)
    path = tmp_path / "ending.py"
    path.write_text('def ending(conn, tid):\n    sql = "UPDATE tasks SET status=\'done\' WHERE id=?"\n    ' + body + '\n')
    assert not _calls_wrapper("ending.py::ending")


def test_receipt_documents_pr1_carrier_not_spike_stub():
    from hermes_cli import kanban_source_receipt as sr
    assert "Spike R1" not in sr.__doc__ and "stub carrier" not in sr.__doc__
    assert "PR-2" in sr.__doc__ and "PR-1" in sr.__doc__


def test_guard_reads_format_and_fstring_sql():
    """The guard must see SQL built with ``.format(...)`` and f-strings (timed_park, apply_block_update)."""
    tree = ast.parse('def f(x):\n    a = "UPDATE tasks SET status = \'todo\' WHERE id = ?".format(1)\n'
                     '    b = f"UPDATE tasks SET status = \'{x}\' WHERE id = ?"\n')
    texts = [_text(n) for n in ast.walk(tree) if isinstance(n, (ast.Call, ast.JoinedStr))]
    assert sum(1 for t in texts if t and _leaves_running(t)) == 2
