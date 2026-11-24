"""Request ETA history work is per board/kind, not per displayed target. [daveey]"""
import sqlite3
from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_external_activity as external
from hermes_cli import kanban_wait_history as history
from hermes_cli import kanban_wait_info as info
from tests.hermes_cli import test_kanban_wait_eta as helpers


@pytest.fixture(autouse=True)
def clear():
    history.reset_for_tests()
    external.reset_for_tests()
    yield
    history.reset_for_tests()
    external.reset_for_tests()


def seed(conn, count):
    conn.executescript("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT, created_at REAL);"
                       "CREATE INDEX idx_events_task ON task_events(task_id, created_at);"
                       "CREATE INDEX idx_events_kind_task_id ON task_events(kind, task_id, id);"
                       "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, block_kind TEXT, human_request TEXT, awaiting TEXT, title TEXT);")
    for i in range(count):
        for j in range(3):
            helpers._finished(conn, f'h{i}-{j}', 'ci', f'https://github.com/acme/repo{i}/actions/runs/{j}', 600 + j * 300)
        helpers._park(conn, f't_{i}', 'ci', f'https://github.com/acme/repo{i}/actions/runs/99')
    # Unrelated newer events make the old id-ordered correlated next-end lookup expensive.
    conn.executemany("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES ('noise','external_watch','{}',1)",
                     [()] * 10_000)
    conn.commit()


@pytest.mark.parametrize('count', [1, 40])
def test_profiles_http_many_eta_targets_have_bounded_queries_and_vm_work(tmp_path, monkeypatch, record_property, count):
    from hermes_cli.web_routers import profiles
    path = tmp_path / 'board.db'

    with closing(sqlite3.connect(path)) as conn:
        seed(conn, count)
    pairs = [(f's{i}', f't_{i}') for i in range(count)]
    statements = []
    steps = [0]

    def progress():
        steps[0] += 100
        return 0

    def open_ro(_path):
        conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        conn.set_trace_callback(statements.append)
        conn.set_progress_handler(progress, 100)
        return conn

    def annotate(rows, **kwargs):
        external.stamp_boards(rows, lambda: [path], open_ro, lambda conn: pairs,
                              lambda row: [row['id']], signature=None)

    monkeypatch.setattr(profiles, '_profile_targets', lambda label: [('default', None)])
    monkeypatch.setattr(profiles, '_read_serial_profile_db', lambda *args: (
        [{'id': sid, 'source': 'cli', 'started_at': 100} for sid, _ in pairs], count))
    monkeypatch.setattr(profiles, '_corrupt_profile_stores', lambda targets: {})
    monkeypatch.setattr(profiles, 'annotate_kanban_activity', annotate)
    for name in ('goal', 'cron', 'background', 'turn', 'subagent'):
        monkeypatch.setattr(profiles, f'annotate_{name}_activity', lambda rows: None)
    monkeypatch.setattr(profiles, 'annotate_review_wait', lambda rows: None)
    app = FastAPI()
    app.include_router(profiles.sessions_router)
    with TestClient(app) as client:
        response = client.get('/api/profiles/sessions?limit=50')
    assert response.status_code == 200, response.text
    sessions = response.json()['sessions']
    assert len(sessions) == count
    # Ensure the actual external/ETA path ran, not merely the HTTP wrapper.
    assert all(row['kanban_external_cards'][0]['eta']['expected_s'] == 900 for row in sessions)
    print(f'GET targets={count} queries={len(statements)} vm_steps={steps[0]}')
    record_property('board_queries', len(statements))
    record_property('vm_steps', steps[0])
    assert len(statements) <= 30, len(statements)
    assert steps[0] < 500_000, steps[0]


def test_overlay_same_target_repeated_is_one_capture():
    with closing(sqlite3.connect(':memory:')) as conn:
        seed(conn, 1)
        waits = {str(i): {'wait_kind': 'ci', 'wait_target': 'acme/repo0', 'waited_since': helpers.PARK}
                 for i in range(600)}
        queries = []
        conn.set_trace_callback(queries.append)
        history.overlay(conn, waits, now=helpers.NOW)
        assert all(w['expected_s'] == 900 for w in waits.values())
        assert len(queries) <= 3, len(queries)


def test_overlay_matches_literal_for_both_kinds_and_first_end_order():
    with closing(helpers._board()) as conn:
        for n in range(25):
            helpers._finished(conn, f't{n}', 'ci', helpers.REPO.format(n=n), 900 + n)
        for n in range(3):
            helpers._finished(conn, f'd{n}', 'deploy', 'hub/core#1', 300 + n, start_kind='continued')
        # The next end is chosen by event id, not minimum timestamp.
        conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES ('t24','claimed','{}',1)")
        waits = {kind: {'wait_kind': kind, 'wait_target': target, 'waited_since': helpers.PARK}
                 for kind, target in [('ci', 'acme/widgets'), ('deploy', 'hub/core'), ('ci-empty', '')]}
        expected = {}
        for key, wait in waits.items():
            if wait['wait_kind'] in ('ci', 'deploy'):
                expected[key] = info.eta_fields(wait['wait_kind'], helpers.PARK,
                    samples=history.samples(conn, wait['wait_kind'], wait['wait_target'], now=helpers.NOW),
                    target=wait['wait_target'])
        history.overlay(conn, waits, now=helpers.NOW)
        for key, fields in expected.items():
            assert all(waits[key][k] == v for k, v in fields.items())
        assert 'expected_s' not in waits['ci-empty']


@pytest.mark.parametrize('blob', [False, True])
def test_display_watch_matches_literal_shell_freshness_and_repark(blob):
    from hermes_cli import kanban_external_watch as watch
    with closing(sqlite3.connect(':memory:')) as conn:
        seed(conn, 2)
        conn.executescript("CREATE TABLE task_wake_checks(task_id TEXT PRIMARY KEY,cmd TEXT,every INTEGER,last_at REAL);")
        conn.execute("INSERT INTO task_wake_checks VALUES ('t_0','true',900,?)", (helpers.NOW,))
        conn.execute("UPDATE task_events SET payload=? WHERE task_id='t_0' AND kind='scheduled'",
                     ('{"awaiting":{"kind":"external","ref":"job"},"wake_check":"true"}',))
        conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES ('t_0','external_watch',?,?)",
                     ('{"host":"local","adapter":"shell","job":"running","service":"ok","every":900,"observed_at":1}', helpers.PARK))
        # An older record must not survive a later continued park.
        conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES ('t_1','external_watch','{}',1)")
        conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES ('t_1','continued',?,?)",
                     ('{"awaiting":{"kind":"ci","ref":"https://github.com/acme/repo1/actions/runs/1"}}', helpers.PARK))
        if blob:
            conn.execute('UPDATE task_events SET payload=CAST(payload AS BLOB)')
        conn.commit()
        hosts = watch.hosts_seen(conn)
        expected = {tid: watch.card_state(conn, tid, now=helpers.NOW, hosts=hosts) for tid in ('t_0', 't_1')}
        assert expected['t_0']['external']['observed_at'] == helpers.NOW
        assert watch.card_states(conn, expected, now=helpers.NOW) == expected
        assert not conn.in_transaction
        conn.execute('BEGIN')
        assert watch.card_states(conn, expected, now=helpers.NOW) == expected
        assert conn.in_transaction
        conn.rollback()


def test_blob_eta_history_matches_literal():
    with closing(sqlite3.connect(':memory:')) as conn:
        seed(conn, 1)
        conn.execute('UPDATE task_events SET payload=CAST(payload AS BLOB)')
        expected = history.samples(conn, 'ci', 'acme/repo0', now=helpers.NOW)
        assert expected == [1200, 900, 600]
        waits = {'x': {'wait_kind': 'ci', 'wait_target': 'acme/repo0', 'waited_since': helpers.PARK}}
        history.overlay(conn, waits, now=helpers.NOW)
        assert waits['x']['expected_s'] == 900


def test_legacy_wake_table_without_last_at_keeps_literal_plan():
    from hermes_cli import kanban_external_watch as watch
    with closing(sqlite3.connect(':memory:')) as conn:
        seed(conn, 1)
        conn.execute('CREATE TABLE task_wake_checks(task_id TEXT PRIMARY KEY,cmd TEXT,every INTEGER)')
        conn.execute("INSERT INTO task_wake_checks VALUES ('t_0','true',900)")
        conn.execute("UPDATE task_events SET payload=? WHERE task_id='t_0' AND kind='scheduled'",
                     ('{"awaiting":{"kind":"external","ref":"job"}}',))
        conn.commit()
        expected = watch.card_state(conn, 't_0', now=helpers.NOW, hosts={})
        assert expected is not None
        assert watch.card_states(conn, ['t_0'], now=helpers.NOW) == {'t_0': expected}


@pytest.mark.parametrize('reverse', [False, True])
def test_history_scan_cap_and_next_call_mutation_match_literal(reverse):
    with closing(helpers._board()) as conn:
        conn.execute(f'PRAGMA reverse_unordered_selects={int(reverse)}')
        for i in range(history.SCAN_CAP + 20):
            helpers._finished(conn, 'reused-card', 'ci', helpers.REPO.format(n=i), 600 if i < 20 else 900)
        # Leave the newest park unfinished: it must not become a sample.
        helpers._park(conn, 'open', 'ci', helpers.REPO.format(n=999))
        expected = history.samples(conn, 'ci', 'acme/widgets', now=helpers.NOW)
        waits = {'x': {'wait_kind': 'ci', 'wait_target': 'acme/widgets', 'waited_since': helpers.PARK}}
        history.overlay(conn, waits, now=helpers.NOW)
        assert waits['x']['expected_s'] == 900
        assert expected == [900] * info.ETA_HISTORY
        conn.execute('UPDATE task_events SET created_at=created_at+500 WHERE kind=\'condition_met\'')
        history.overlay(conn, waits, now=helpers.NOW)
        assert waits['x']['expected_s'] == 1400  # no process-global stale target memo


def test_display_capture_snapshot_and_next_invocation(tmp_path, monkeypatch):
    from hermes_cli import kanban_external_display as display
    from hermes_cli import kanban_external_watch as watch
    path = tmp_path / 'wal.db'
    with closing(sqlite3.connect(path)) as reader, closing(sqlite3.connect(path)) as writer:
        reader.execute('PRAGMA journal_mode=WAL')
        seed(reader, 1)
        expected = watch.card_state(reader, 't_0', now=helpers.NOW, hosts={})
        original = display.Capture
        def capture(conn, ids):
            # Status/hosts have pinned the read snapshot already.
            writer.execute("UPDATE tasks SET status='done'")
            writer.execute("UPDATE task_events SET payload='{}' WHERE task_id='t_0'")
            writer.commit()
            return original(conn, ids)
        monkeypatch.setattr(display, 'Capture', capture)
        assert watch.card_states(reader, ['t_0'], now=helpers.NOW) == {'t_0': expected}
        assert not reader.in_transaction
        assert watch.card_states(reader, ['t_0'], now=helpers.NOW) == {}
