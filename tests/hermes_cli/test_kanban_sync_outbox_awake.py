"""Outbox alarms require an observed sendable identity in awake time."""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_sync_backlog as backlog
from hermes_cli import kanban_sync_outbox_age as alarm
from tests.hermes_cli import test_kanban_sync_push_starvation as fixtures

kanban_home = fixtures.kanban_home
live = fixtures.live


@pytest.fixture
def host(live, kanban_home, monkeypatch):
    alarm.reset()
    backlog.reset()
    clock = SimpleNamespace(wall=10000.0, mono=100.0)
    monkeypatch.setattr(alarm.time, "time", lambda: clock.wall)
    monkeypatch.setattr(alarm.replica_age, "check", lambda *a, **kw: [])
    path = kanban_home / "kanban.db"
    backlog.note(path, False)
    kb.create_task(live, title="unsent")
    live.execute("UPDATE kanban_sync_outbox SET created_at = ?", (clock.wall * 1000,))
    boards = [("default", path)]
    ok = [SimpleNamespace(board="default", error=None, behind=False)]
    calls = []
    def after():
        return alarm.after_round(ok, boards, mono=lambda: clock.mono)
    def check():
        return alarm.check(boards=boards, mono=lambda: clock.mono,
                           file_fn=lambda s, d: calls.append(d) or "t_alarm")
    yield SimpleNamespace(clock=clock, after=after, check=check, calls=calls, conn=live, path=path)
    alarm.reset()
    backlog.reset()


@pytest.mark.parametrize("entry", ["after", "check"])
def test_sleep_wall_jump_does_not_alarm(host, entry):
    assert host.after() == []
    assert host.check() == []
    host.clock.wall += 660
    host.clock.mono += 60
    assert getattr(host, entry)() == []
    assert host.calls == []


def _advance(host, seconds):
    host.clock.wall += seconds
    host.clock.mono += seconds


@pytest.mark.parametrize("entry", ["after", "check"])
def test_same_op_alarms_only_after_600_awake_seconds(host, entry, caplog):
    assert host.after() == []
    _advance(host, 599)
    assert getattr(host, entry)() == []
    _advance(host, 1 if entry == "after" else 60)
    assert getattr(host, entry)() == (["default"] if entry == "after" else ["t_alarm"])
    _advance(host, 60)
    assert getattr(host, entry)() == []
    text = caplog.text if entry == "after" else host.calls[0]
    op_id = host.conn.execute("SELECT op_id FROM kanban_sync_outbox ORDER BY created_at, rowid").fetchone()[0]
    assert op_id in text and "wall" in text and "observed-awake" in text


@pytest.mark.parametrize("change", ["identity", "ack", "held", "retry", "unreadable"])
def test_losing_sendable_identity_restarts_budget(host, change):
    host.after()
    _advance(host, 500)
    assert host.check() == []
    statements = {
        "identity": "UPDATE kanban_sync_outbox SET op_id = op_id || '-replacement'",
        "ack": "UPDATE kanban_sync_outbox SET state = 'acked'",
        "held": "UPDATE kanban_sync_outbox SET table_name = 'task_runs'",
        "retry": "UPDATE kanban_sync_outbox SET retry_after = 1",  # expired still answered
        "unreadable": "ALTER TABLE kanban_sync_outbox RENAME TO hidden_outbox",
    }
    host.conn.execute(statements[change])
    if change == "held":
        backlog.note(host.path, True)
    _advance(host, 60)
    assert host.check() == []
    if change == "unreadable":
        host.conn.execute("ALTER TABLE hidden_outbox RENAME TO kanban_sync_outbox")
    host.conn.execute("UPDATE kanban_sync_outbox SET state = 'pending', retry_after = NULL")
    backlog.note(host.path, False)
    _advance(host, 60)
    assert host.after() == []
    assert host.check() == []
    _advance(host, 600)
    assert host.check() == ["t_alarm"]


@pytest.mark.parametrize("restart", [False, True])
def test_observation_gap_over_bound_resets(host, restart):
    host.after()
    _advance(host, 500)
    host.after()
    if restart:
        alarm.reset()
    _advance(host, 601)
    assert host.after() == []
    assert host.check() == []


def test_restart_same_boot_keeps_budget_and_incident(host):
    host.after()
    _advance(host, 500)
    host.after()
    alarm.reset()
    _advance(host, 100)
    assert host.after() == ["default"]
    assert host.check() == ["t_alarm"]
    alarm.reset()
    _advance(host, 60)
    assert host.after() == []
    assert host.check() == []


@pytest.mark.parametrize("boot", ["other-boot", None])
def test_new_or_unknown_boot_cannot_resume(host, monkeypatch, boot):
    host.after()
    _advance(host, 500)
    host.after()
    alarm.reset()
    monkeypatch.setattr(alarm.replica_age, "_boot_id", lambda: boot)
    _advance(host, 100)
    assert host.after() == []
    assert host.check() == []


def test_failed_report_is_retried_after_restart(host):
    host.after()
    _advance(host, 600)
    assert alarm.check(boards=[("default", host.path)], mono=lambda: host.clock.mono,
                       file_fn=lambda *a: None) == []
    alarm.reset()
    _advance(host, 60)
    host.after()
    assert host.check() == ["t_alarm"]


def test_timestamp_is_taken_after_sampling(host, monkeypatch):
    original = alarm._sample
    def delayed(path):
        _advance(host, 500)
        return original(path)
    monkeypatch.setattr(alarm, "_sample", delayed)
    assert host.after() == []
    monkeypatch.setattr(alarm, "_sample", original)
    _advance(host, 100)
    assert host.after() == []
    assert host.check() == []


@pytest.mark.parametrize("hold", ["fifo", "stale-wake", "future-retry"])
def test_only_sendable_rows_contribute(host, hold):
    ids = host.conn.execute("SELECT op_id, row_uid FROM kanban_sync_outbox ORDER BY created_at, rowid").fetchall()
    first, uid = ids[0]
    if hold == "fifo":
        host.conn.execute("UPDATE kanban_sync_outbox SET table_name = 'task_runs' WHERE op_id = ?", (first,))
    if hold == "stale-wake":
        host.conn.execute("UPDATE tasks SET sync_version = 1")
        host.conn.execute("UPDATE kanban_sync_outbox SET table_name = 'tasks', row_uid = ?, "
                          "fields = '{\"status\":\"ready\"}'", (uid,))
        backlog.round_evidence(host.path)[uid] = True
    if hold == "future-retry":
        host.conn.execute("UPDATE kanban_sync_outbox SET retry_after = ?", (host.clock.wall + 10000,))
    backlog.note(host.path, True)
    assert alarm._sample(host.path) is None
    assert host.after() == []
    _advance(host, 600)
    assert host.after() == []
    assert host.check() == []
