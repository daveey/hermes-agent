"""Ready age starts at release, never at the beginning of a spend hold."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from hermes_cli import kanban_admission_score as score
from hermes_cli import kanban_chat_wait as cw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_parent_unclaimed as pu
from tests.hermes_cli import test_kanban_ready_wait_checkpoint as base
from tests.hermes_cli import test_kanban_spend_promote as spend

conn = base.conn
fleet = spend.fleet


def _assert_release(conn, tid, at):
    assert pu.ready_since(conn, tid, at - 10800) == at
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    assert score.rank(conn, [row], now_ms=(at + 4) * 1000)[0][1].age_buckets == 0
    base._subscribe(conn, tid)
    assert cw.sweep(conn, now=at + 4) == 0
    assert cw.sweep(conn, now=at + cw.THRESHOLD_SECONDS - 1) == 0
    assert cw.sweep(conn, now=at + cw.THRESHOLD_SECONDS) == 1
    assert cw.sweep(conn, now=at + cw.THRESHOLD_SECONDS + 1) == 0


def test_manual_promote_resets_three_hour_spend_hold(conn, monkeypatch):
    at = 1795292854
    monkeypatch.setattr("time.time", lambda: at - 10800)
    tid = kb.create_task(conn, title="bounded work", assignee="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "spend_budget_triaged", {"reason": "runs"})
    monkeypatch.setattr("time.time", lambda: at)
    assert kb.promote_task(conn, tid, actor="user")[0]
    _assert_release(conn, tid, at)


def test_spend_promote_resets_three_hour_spend_hold(fleet, monkeypatch):
    f = fleet
    import time

    at = int(time.time())
    episode = spend._stop(f, at=at - 10800)
    # Do not rewrite signed courier events or the fixture caller's live lease.
    with kb.write_txn(f.h):
        f.h.execute("UPDATE tasks SET created_at=? WHERE id=?", (at - 10800, f.target))
        f.h.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='created'",
                    (at - 10800, f.target))
    cid = spend._decision_comment(f)
    at = int(time.time())
    monkeypatch.setattr("time.time", lambda: at)
    spend._submit(f, spend._op(f, episode, cid))
    spend._round(f)
    assert kb.get_task(f.h, f.target).status == "ready"
    _assert_release(f.h, f.target, at)


def test_long_wait_after_promotion_notifies_exactly_once(conn, monkeypatch):
    at = 1795292854
    monkeypatch.setattr("time.time", lambda: at - 10800)
    tid = kb.create_task(conn, title="long promotion wait", assignee="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "spend_budget_triaged", {})
    monkeypatch.setattr("time.time", lambda: at)
    assert kb.promote_task(conn, tid, actor="user")[0]
    base._subscribe(conn, tid)
    assert cw.sweep(conn, now=at + cw.THRESHOLD_SECONDS) == 1
    assert cw.sweep(conn, now=at + cw.THRESHOLD_SECONDS + 1) == 0


# Independently enumerate the audited entry paths, not the registry under test.
@pytest.mark.parametrize("kind", [
    "created", "promoted", "promoted_manual", "triage_swept", "containment_released",
    "review_reopened", "reopened", "changes_requested", "reclaimed", "unblocked",
    "continuation_woken", "condition_met", "wake_probe_stale", "wake_check_stale", "owner_restart_lost",
    "wake_check_broken", "wake_check_lost", "ci_run_unstartable",
    "wake_check_dead_ancestry", "external_ref_broken", "external_service_down",
    "supervisor_request_wake", "shared_blocker_cleared",
])
def test_ready_entry_path_floor(conn, kind):
    tid = kb.create_task(conn, title=kind, assignee="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET created_at=100 WHERE id=?", (tid,))
        conn.execute("UPDATE task_events SET created_at=100 WHERE task_id=?", (tid,))
        kb._append_event(conn, tid, kind, {"status": "ready"})
        conn.execute("UPDATE task_events SET created_at=200 WHERE task_id=? AND kind=?", (tid, kind))
    assert pu.ready_since(conn, tid, 100) >= 200
    assert score._ready_since(conn, [tid])[tid] >= 200
    assert f"'{kind}'" in cw._floor_kinds()


# Non-entry observations in status-writing functions. New event kinds require an
# explicit audit even when a writer uses a bound status parameter, different SQL
# whitespace, or a bare (rather than facade-qualified) _append_event call.
NON_ENTRY_WRITER_EVENTS = {
    "decision_retained",  # stop-only custody, never ready admission
    "continuation_exhausted", "blocked", "spend_budget_held", "gave_up",
    "dependency_wait", "linked", "claim_rejected", "completed", "review_requested",
    "descendant_invalidated", "status", "scheduled", "assigned", "supervisor_escalated",
    "archive_worker_termination", "archived", "request_withdrawn", "claimed",
    "agent_block_escalated", "timed_out", "stale", "reconciled", "parent_found",
    "parent_missing", "status_healed",
    # Preclaim observation; exhaustion moves to triage, never releases to ready.
    "review_admission_refused",
    # Safety observations close/retain a stopped run, never enter ready.
    "safety_refused", "decision_hold_restricted",
}


@pytest.mark.parametrize("kind", ["review_admission_refused", "safety_refused", "decision_hold_restricted"])
def test_nonentry_observation_does_not_reset_ready_age(conn, kind):
    tid = kb.create_task(conn, title="review refusal observation", assignee="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET created_at=100 WHERE id=?", (tid,))
        conn.execute("UPDATE task_events SET created_at=100 WHERE task_id=?", (tid,))
        kb._append_event(conn, tid, kind, {"preclaim": True})
        conn.execute(
            "UPDATE task_events SET created_at=200 WHERE task_id=? AND kind=?",
            (tid, kind),
        )
    assert pu.ready_since(conn, tid, 100) == 100
    assert score._ready_since(conn, [tid])[tid] == 100
    assert f"'{kind}'" not in cw._floor_kinds()


def _writer_events(source):
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        strings = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        if not any(re.search(r"UPDATE\s+tasks\s+SET\s+(?:(?!WHERE).)*\bstatus\s*=", s, re.IGNORECASE | re.DOTALL)
                   for s in strings):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call) or len(call.args) < 3:
                continue
            name = (call.func.attr if isinstance(call.func, ast.Attribute)
                    else call.func.id if isinstance(call.func, ast.Name) else "")
            if name == "_append_event" and isinstance(call.args[2], ast.Constant):
                yield call.args[2].value


def test_status_writers_register_new_event_kinds():
    from hermes_cli.kanban_ready_events import READY_ENTRY_EVENT_KINDS

    found = set()
    for path in Path(kb.__file__).parent.glob("kanban*.py"):
        events = set(_writer_events(path.read_text()))
        assert events <= READY_ENTRY_EVENT_KINDS | NON_ENTRY_WRITER_EVENTS, (path.name, events)
        found |= events
    assert {"promoted_manual", "triage_swept", "review_reopened", "reopened", "changes_requested"} <= found


@pytest.mark.parametrize("sql", ["status='ready'", "status = ?"])
@pytest.mark.parametrize("writer", ["_append_event", "kb._append_event"])
def test_writer_inventory_detects_unregistered_new_kind(sql, writer):
    source = f'''def new_path(conn):
        conn.execute("UPDATE tasks SET {sql} WHERE id=?")
        {writer}(conn, tid, "new_ready_path", {{}})
    '''
    from hermes_cli.kanban_ready_events import READY_ENTRY_EVENT_KINDS

    assert set(_writer_events(source)) - READY_ENTRY_EVENT_KINDS - NON_ENTRY_WRITER_EVENTS == {"new_ready_path"}


def _wake_events(source, modules):
    """Resolve literal/constant wake kinds statically; never import maintenance code.

    Lookup follows the call's lexical scopes, including function-local imports.
    Ambiguous bindings, cycles and unsupported expressions fail closed as None.
    """
    trees = {}
    scope_bindings = {}

    def module_scope(name):
        if name not in modules:
            return None
        if name not in trees:
            trees[name] = ast.parse(modules[name])
        return bindings(trees[name])

    def bindings(scope):
        if scope in scope_bindings:
            return scope_bindings[scope]
        result = {}
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in (*scope.args.posonlyargs, *scope.args.args, *scope.args.kwonlyargs):
                result[arg.arg] = [None]

        def visit(node):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                return
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        result.setdefault(target.id, []).append(node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                result.setdefault(node.target.id, []).append(node.value)
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    result.setdefault(alias.asname or alias.name, []).append((node.module, alias.name))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    result.setdefault(alias.asname or alias.name.split('.')[0], []).append(
                        (alias.name if alias.asname else alias.name.split('.')[0], None))
            for child in ast.iter_child_nodes(node):
                visit(child)

        for node in scope.body:
            visit(node)
        scope_bindings[scope] = result
        return result

    def resolve(expr, scopes, seen=frozenset()):
        if isinstance(expr, ast.Constant):
            return expr.value if isinstance(expr.value, str) else None
        if isinstance(expr, ast.Attribute):
            module = resolve(expr.value, scopes, seen)
            if isinstance(module, tuple) and module[0] in modules:
                return resolve(ast.Name(id=expr.attr), [module_scope(module[0])], seen)
            return None
        if not isinstance(expr, ast.Name):
            return None
        for scope in reversed(scopes):
            if expr.id not in scope:
                continue
            # Repeated identical local imports are common in separate branches.
            candidates = list({ast.dump(v) if isinstance(v, ast.AST) else v: v
                               for v in scope[expr.id]}.values())
            key = (id(scope), expr.id)
            if key in seen or len(candidates) != 1:
                return None
            value = candidates[0]
            if isinstance(value, tuple):
                module, name = value
                if name is None:
                    return (module,)
                if module == 'hermes_cli' and 'hermes_cli.' + name in modules:
                    return ('hermes_cli.' + name,)
                if module in modules:
                    return resolve(ast.Name(id=name), [module_scope(module)], seen | {key})
                return None
            return resolve(value, scopes, seen | {key})
        return None

    def walk(node, scopes):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scopes = [*scopes, bindings(node)]
        if isinstance(node, ast.Call):
            name = (node.func.attr if isinstance(node.func, ast.Attribute)
                    else node.func.id if isinstance(node.func, ast.Name) else '')
            if name == '_wake':
                expr = node.args[2] if len(node.args) > 2 else next(
                    (kw.value for kw in node.keywords if kw.arg == 'kind'), None)
                value = resolve(expr, scopes)
                yield node.lineno, value if isinstance(value, str) else None
        for child in ast.iter_child_nodes(node):
            yield from walk(child, scopes)

    yield from walk(ast.parse(source), [])


def test_wake_callers_register_event_kinds():
    from hermes_cli import kanban_ready_events as kre

    registered = kre.READY_ENTRY_EVENT_KINDS | getattr(kre, "READY_ENTRY_CONDITIONAL_KINDS", frozenset())
    paths = sorted(Path(kb.__file__).parent.glob('kanban*.py'))
    modules = {'hermes_cli.' + p.stem: p.read_text() for p in paths}
    found, failures = set(), []
    for path in paths:
        for line, kind in _wake_events(modules['hermes_cli.' + path.stem], modules):
            found.add(kind)
            if kind not in registered:
                failures.append((path.name, line, kind))
    assert not failures, failures
    assert {'condition_met', 'wake_check_lost', 'wake_check_stale', 'ci_run_unstartable'} <= found
    assert 'shared_blocker_cleared' in getattr(kre, "READY_ENTRY_CONDITIONAL_KINDS", frozenset())
    assert 'shared_blocker_cleared' not in kre.READY_ENTRY_EVENT_KINDS


@pytest.mark.parametrize('writer', ['_wake', 'kwc._wake'])
@pytest.mark.parametrize('expression, expected', [
    ('NEW_KIND', 'new_ready_path'), ('missing', None), ('factory()', None),
    ('kwc.NEW_KIND', 'new_ready_path'), ('episode.NEW_KIND', 'new_ready_path'),
])
def test_wake_inventory_detects_unregistered_new_kind(writer, expression, expected):
    from hermes_cli.kanban_ready_events import READY_ENTRY_EVENT_KINDS

    source = f'''from hermes_cli import kanban_wake_check as kwc
NEW_KIND = "new_ready_path"
def new_path(conn, tid):
    from hermes_cli import kanban_wake_episode as episode
    {writer}(conn, tid, {expression}, {{}})
'''
    modules = {name: 'NEW_KIND = "new_ready_path"' for name in (
        'hermes_cli.kanban_wake_check', 'hermes_cli.kanban_wake_episode')}
    assert list(_wake_events(source, modules)) == [(5, expected)]
    assert expected not in READY_ENTRY_EVENT_KINDS


@pytest.mark.parametrize('source, expected', [
    ('KIND = "condition_met"\n_wake(conn, tid, KIND, {})', 'condition_met'),
    ('from hermes_cli.kanban_wake_check import CONDITION_MET_EVENT as KIND\n'
     '_wake(conn, tid, KIND, {})', 'condition_met'),
    ('import hermes_cli.kanban_wake_check as kwc\n'
     'kwc._wake(conn, tid, kwc.CONDITION_MET_EVENT, {})', 'condition_met'),
    ('def path(conn, tid):\n'
     '    from hermes_cli.kanban_wake_check import CONDITION_MET_EVENT as KIND\n'
     '    from hermes_cli.kanban_wake_check import CONDITION_MET_EVENT as KIND\n'
     '    _wake(conn, tid, KIND, {})', 'condition_met'),
    ('KIND: str = "condition_met"\n_wake(conn, tid, kind=KIND, payload={})', 'condition_met'),
    ('KIND = "condition_met"\ndef path(conn, tid, KIND):\n    _wake(conn, tid, KIND, {})', None),
    ('KIND = "condition_met"\nKIND = "new_kind"\n_wake(conn, tid, KIND, {})', None),
    ('A = B\nB = A\n_wake(conn, tid, A, {})', None),
    ('_wake(conn, tid, 42, {})', None),
    ('_wake(conn, tid)', None),
])
def test_wake_inventory_resolves_constants_or_fails_closed(source, expected):
    modules = {'hermes_cli.kanban_wake_check': 'CONDITION_MET_EVENT = "condition_met"'}
    assert [kind for _, kind in _wake_events(source, modules)] == [expected]


def test_shared_blocker_cleared_without_status_keeps_ready_clock(conn):
    """Already-ready hold retirement is not a ready entry (green on base and after)."""
    tid = kb.create_task(conn, title="already-ready hold retirement", assignee="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET created_at=100 WHERE id=?", (tid,))
        conn.execute("UPDATE task_events SET created_at=100 WHERE task_id=?", (tid,))
        kb._append_event(conn, tid, "shared_blocker_cleared", {"blocker_id": "quota"})
        conn.execute(
            "UPDATE task_events SET created_at=200 WHERE task_id=? AND kind=?",
            (tid, "shared_blocker_cleared"),
        )
    assert pu.ready_since(conn, tid, 100) == 100
    assert score._ready_since(conn, [tid])[tid] == 100


def test_lost_probe_wake_starts_new_ready_interval(conn, monkeypatch):
    from hermes_cli import kanban_continuation as kc
    from hermes_cli import kanban_wake_check as kwc

    t0, t1 = 1795470116, 1795483617
    monkeypatch.setattr('time.time', lambda: t0)
    tid = kb.create_task(conn, title='lost probe after long park', assignee='default')
    assert kb.claim_task(conn, tid)
    parked = kc.continue_task(conn, tid, checkpoint='waiting for external result',
                              resume_after_seconds=20000, clock_only=True,
                              awaiting={'kind': 'clock', 'why': 'bounded external-result backstop'})
    assert parked['ok'], parked
    assert kb.get_task(conn, tid).status == 'scheduled'
    assert conn.execute('SELECT ended_at FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0] == t0
    base._subscribe(conn, tid)
    monkeypatch.setattr('time.time', lambda: t1)
    with kb.write_txn(conn):
        assert kwc._wake(conn, tid, kwc.LOST_EVENT, {'reason': 'remote_probe_silent'}) == kwc.LOST_EVENT
    assert kb.get_task(conn, tid).status == 'ready'
    assert pu.ready_since(conn, tid, t0) == t1
    assert score._ready_since(conn, [tid])[tid] == t1
    assert cw.sweep(conn, now=t1) == 0
    assert cw.sweep(conn, now=t1 + cw.THRESHOLD_SECONDS - 1) == 0
    assert cw.sweep(conn, now=t1 + cw.THRESHOLD_SECONDS) == 1
    assert cw.sweep(conn, now=t1 + cw.THRESHOLD_SECONDS + 1) == 0
