"""Authoritative source snapshots through the real card consumers (isolated SQLite)."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_execution_observations as observations
from hermes_cli import kanban_session_activity as activity
from hermes_cli.execution_observations import project
from tests.hermes_cli import test_kanban_session_activity as fixtures

kanban_root = fixtures.kanban_root


@pytest.fixture
def board(kanban_root, monkeypatch):
    monkeypatch.setattr(observations, "local_scope", lambda: ("fixture-gateway", "default"))
    monkeypatch.setattr(activity.time, "time", lambda: 1000)
    path = kanban_root / "kanban.db"
    fixtures._make_full_board(path, [("card", "Work", "ready", "agent", "session", 10, None, None)])
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT, created_at INTEGER, payload TEXT)")
        conn.execute("INSERT INTO task_events VALUES (1, 'card', NULL, 'created', 10, ?)", (json.dumps({"status": "ready"}),))
    return path


def test_old_queued_genesis_is_fresh_local_snapshot_and_cache_expires(board, monkeypatch):
    value = activity.session_tasks(["session"])[0]["execution_observations"][0]
    assert value["state"] == "queued"
    assert value["observed_at"] == 1000
    assert value["identity"]["generation"].startswith("local:")
    assert value["phase_since"] == 10
    assert project(value, now=1180)["state"] == "unknown"
    assert value["last_progress"] is None


def transition(path, status, kind, payload):
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE tasks SET status=?", (status,))
        conn.execute("INSERT INTO task_events (task_id, kind, created_at, payload) VALUES ('card', ?, 990, ?)",
                     (kind, json.dumps(payload) if payload is not None else None))


def test_clock_review_human_waits_have_exact_conditions(board):
    transition(board, "scheduled", "scheduled", {"wake_at": 1100, "clock_only": True})
    value = activity.session_tasks(["session"])[0]["execution_observations"][0]
    assert value["state"] == "waiting"
    assert value["wait"]["predicate"] == "clock >= wake_at"
    assert value["wait"]["due_at"] == 1100
    transition(board, "review", "review_requested", {"reviewer": "agent", "implementer": "writer"})
    value = activity.session_tasks(["session"])[0]["execution_observations"][0]
    assert value["state"] == "waiting" and value["wait"]["kind"] == "review"
    request = {"action": "Approve", "why_human": "Owner needed", "interaction": "approval", "scope": "fixture", "evidence": "fixture"}
    with sqlite3.connect(board) as conn:
        conn.execute("ALTER TABLE tasks ADD COLUMN block_kind TEXT")
        conn.execute("ALTER TABLE tasks ADD COLUMN human_request TEXT")
        conn.execute("UPDATE tasks SET block_kind='needs_input', human_request=?", (json.dumps(request),))
    transition(board, "blocked", "blocked", {"kind": "needs_input", "human_request": request})
    value = activity.session_tasks(["session"])[0]["execution_observations"][0]
    assert value["state"] == "needs_user" and value["wait"]["predicate"] == "request resolved by unblock"
    assert project(value, now=1180)["human_request"] == request


def test_running_uses_joined_run_not_task_heartbeat(board):
    with sqlite3.connect(board) as conn:
        conn.execute("ALTER TABLE tasks ADD COLUMN last_heartbeat_at INTEGER")
        conn.execute("ALTER TABLE task_runs ADD COLUMN last_heartbeat_at INTEGER")
        conn.execute("ALTER TABLE task_runs ADD COLUMN status TEXT")
        conn.execute("ALTER TABLE task_runs ADD COLUMN ended_at INTEGER")
        conn.execute("INSERT INTO task_runs VALUES (7, 'card', 900, 990, 'running', NULL)")
        conn.execute("UPDATE tasks SET status='running', current_run_id=7, last_heartbeat_at=999")
    value = activity.session_tasks(["session"])[0]["execution_observations"][0]
    assert value["state"] == "running" and value["observed_at"] == 990
    with sqlite3.connect(board) as conn:
        conn.execute("UPDATE task_runs SET task_id='other'")
    assert activity.session_tasks(["session"])[0]["execution_observations"][0]["state"] == "unknown"


def test_dependency_matches_canonical_remote_and_missing_parents(board):
    from hermes_cli import kanban_db as kb
    transition(board, "todo", "dependency_wait", {"parent": "missing", "reason": "parent_not_done"})
    with sqlite3.connect(board) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
        conn.execute("CREATE TABLE kanban_sync_remote_parents (parent_id TEXT, status TEXT)")
        conn.executemany("INSERT INTO task_links VALUES (?, 'card')", [("missing",), ("remote",)])
        conn.execute("INSERT INTO kanban_sync_remote_parents VALUES ('remote', 'running')")
        expected = kb.parent_states(conn, 'card')
    value = activity.session_tasks(["session"])[0]["execution_observations"][0]
    assert value["state"] == "unknown"  # remote mirror has no qualified owning gateway
    assert [(p['work_id'], p['status']) for p in value['dependency_states']] == expected
    assert len(activity.session_tasks(["session"])) == 1


def test_counts_and_detail_use_same_raw_source_snapshot(board, monkeypatch):
    from hermes_cli.kanban_queue_position import detail_fields
    from dataclasses import make_dataclass
    monkeypatch.setattr('hermes_cli.kanban_queue_position.read', lambda *a: None)
    monkeypatch.setattr('hermes_cli.kanban_capacity_evidence.read', lambda *a, **kw: {})
    rows = [{"id": "session"}]
    activity.annotate_kanban_activity(rows)
    value = rows[0]["execution_observations"][0]
    assert value['state'] == 'queued' and value['observed_at'] == 1000
    assert activity.session_tasks(['session'])[0]['execution_observations'][0] == value
    with sqlite3.connect(board) as conn:
        conn.row_factory = sqlite3.Row
        Task = make_dataclass('Task', [('id', str), ('status', str), ('assignee', str)])
        detail = detail_fields(conn, Task('card', 'ready', 'agent'), 'default')
    assert detail['execution_observations'][0]['identity'] == value['identity']
    assert detail['execution_observations'][0]['state'] == 'queued'
    monkeypatch.setattr(activity.time, 'time', lambda: 1180)
    activity.annotate_kanban_activity(rows)
    expired = rows[0]['execution_observations'][0]
    assert expired['state'] == 'unknown' and expired['observed_at'] == 1000


def test_foreign_replica_row_cannot_use_local_read_or_pull_clock(board):
    with sqlite3.connect(board) as conn:
        conn.execute('ALTER TABLE tasks ADD COLUMN created_on TEXT')
        conn.execute("UPDATE tasks SET created_on='foreign-gateway'")
    value = activity.session_tasks(['session'])[0]['execution_observations'][0]
    assert value['state'] == 'unknown'
    assert value['observed_at'] is None


def test_synced_causal_head_fork_gap_and_uid_remapping(board):
    with sqlite3.connect(board) as conn:
        conn.execute('ALTER TABLE task_events ADD COLUMN uid TEXT')
        conn.execute('ALTER TABLE task_runs ADD COLUMN uid TEXT')
        payload = {'status': 'ready', '_recovery_history_v1': {'identity': 'phase-1', 'predecessors': []}}
        conn.execute("INSERT INTO task_events VALUES (2,'card',NULL,'promoted',990,?,'event-2')", (json.dumps(payload),))
    value = activity.session_tasks(['session'])[0]['execution_observations'][0]
    assert value['state'] == 'queued' and value['identity']['generation'] == 'event:phase-1'
    with sqlite3.connect(board) as conn:
        conn.execute("INSERT INTO task_runs VALUES (77,'card',995,'run-uid')")
    assert activity.session_tasks(['session'])[0]['execution_observations'][0]['state'] == 'unknown'
    with sqlite3.connect(board) as conn:
        conn.execute('DELETE FROM task_runs')
        payload = {'status': 'ready', '_recovery_history_v1': {'identity': 'fork', 'predecessors': []}}
        conn.execute("INSERT INTO task_events VALUES (3,'card',NULL,'promoted',990,?,'event-3')", (json.dumps(payload),))
    assert activity.session_tasks(['session'])[0]['execution_observations'][0]['state'] == 'unknown'


def test_local_dependency_wait_names_all_local_targets_without_owning_them(board):
    transition(board, 'todo', 'dependency_wait', {'reason': 'parent_not_done', 'parent': 'parent'})
    with sqlite3.connect(board) as conn:
        conn.execute('CREATE TABLE task_links (parent_id TEXT, child_id TEXT)')
        conn.execute("INSERT INTO tasks (id,title,status,assignee) VALUES ('parent','Upstream','ready','agent')")
        conn.execute("INSERT INTO task_links VALUES ('parent','card')")
    rows = activity.session_tasks(['session'])
    value = rows[0]['execution_observations'][0]
    assert len(rows) == 1 and value['state'] == 'waiting'
    assert value['wait']['targets'][0]['work_id'] == 'parent'
    assert value['wait']['predicate'] == 'all named parents in done|archived'


def test_human_wait_same_in_counts_and_list_without_private_probe(board):
    request = {'action': 'Approve', 'why_human': 'Owner needed', 'interaction': 'approval', 'scope': 'fixture', 'evidence': 'fixture', 'resolved_check': 'secret-command'}
    with sqlite3.connect(board) as conn:
        conn.execute('ALTER TABLE tasks ADD COLUMN block_kind TEXT')
        conn.execute('ALTER TABLE tasks ADD COLUMN human_request TEXT')
        conn.execute("UPDATE tasks SET block_kind='needs_input', human_request=?", (json.dumps(request),))
    transition(board, 'blocked', 'blocked', {'kind': 'needs_input', 'human_request': request})
    row = {'id': 'session'}
    activity.annotate_kanban_activity([row])
    value = row['execution_observations'][0]
    listed = activity.session_tasks(['session'])[0]['execution_observations'][0]
    assert value['state'] == listed['state'] == 'needs_user'
    assert value['human_request'] == listed['human_request']
    assert 'secret-command' not in repr(activity._read_annotation_parts())


@pytest.mark.parametrize('kind,payload', [('promoted', None), ('promoted_manual', {}), ('triage_swept', {})])
def test_exact_ready_landing_defaults(board, kind, payload):
    transition(board, 'ready', kind, payload)
    assert activity.session_tasks(['session'])[0]['execution_observations'][0]['state'] == 'queued'


def _defect_malformed(conn):
    conn.execute("UPDATE task_events SET payload='{' ")


def _defect_oversize(conn):
    conn.execute('UPDATE task_events SET payload=?', (json.dumps({'status': 'ready', 'extra': 'x' * 33000}),))


def _defect_overflow(conn):
    conn.executemany("INSERT INTO task_events(task_id,kind,payload) VALUES ('card','commented','{}')", [()] * 128)


def _defect_unbound(conn):
    conn.execute("INSERT INTO task_events(task_id,kind,payload) VALUES ('card','condition_met','{}')")


def _defect_mismatch(conn):
    conn.execute("UPDATE task_events SET payload=?", (json.dumps({'status': 'todo'}),))


_DEFECTS = {'malformed': _defect_malformed, 'oversize': _defect_oversize, 'overflow': _defect_overflow,
            'unbound': _defect_unbound, 'mismatch': _defect_mismatch}


@pytest.mark.parametrize('defect', list(_DEFECTS))
def test_unsupported_or_incomplete_phase_remains_unknown(board, defect):
    with sqlite3.connect(board) as conn:
        _DEFECTS[defect](conn)
    assert activity.session_tasks(['session'])[0]['execution_observations'][0]['state'] == 'unknown'


def test_assembler_query_bound_and_disk_copy_preserve_source_clocks(board, monkeypatch):
    from hermes_cli.kanban_execution_source import assemble
    from hermes_cli import kanban_annotation_disk as disk
    with sqlite3.connect(board) as conn:
        conn.execute('CREATE TABLE task_links (parent_id TEXT, child_id TEXT)')
        conn.executemany("INSERT INTO tasks(id,title,status) VALUES (?,'Work','ready')", [(f'c{n}',) for n in range(500)])
        statements = []
        conn.set_trace_callback(statements.append)
        sources = assemble(conn, ['card', *(f'c{n}' for n in range(500))], board='default', gateway='g', profile='p', observed_at=1000)
    assert len(sources) == 501
    assert len([s for s in statements if s.startswith('SELECT')]) == 6
    parts = activity._read_annotation_parts()
    monkeypatch.setattr(disk, 'FORCE', True)
    assert disk.save(('fixture',), parts)
    loaded, _saved_at = disk.load(('fixture',))
    assert loaded[11] == parts[11]
    assert loaded[11]['session']['default/card']['observed_at'] == 1000


def test_synced_running_uid_survives_local_id_remapping(board):
    with sqlite3.connect(board) as conn:
        conn.execute('ALTER TABLE task_events ADD COLUMN uid TEXT')
        for column in ('uid TEXT', 'status TEXT', 'last_heartbeat_at INTEGER', 'ended_at INTEGER'):
            conn.execute('ALTER TABLE task_runs ADD COLUMN ' + column)
        conn.execute("INSERT INTO task_runs VALUES (7,'card',900,'portable-run','running',990,NULL)")
        conn.execute("UPDATE tasks SET status='running', current_run_id=7")
    first = activity.session_tasks(['session'])[0]['execution_observations'][0]
    with sqlite3.connect(board) as conn:
        conn.execute('UPDATE task_runs SET id=77')
        conn.execute('UPDATE tasks SET current_run_id=77')
    second = activity.session_tasks(['session'])[0]['execution_observations'][0]
    assert first['state'] == second['state'] == 'running'
    assert first['identity'] == second['identity']
    assert first['identity']['run_id'] == 'portable-run'
