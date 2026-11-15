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
    from cron import restart_rerun
    clock = SimpleNamespace(wall=10000.0, mono=100.0, awake=True)
    monkeypatch.setattr(restart_rerun, "host_fully_awake", lambda: clock.awake)
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
def test_darkwake_never_counts_then_awake_stall_alarms(host, entry, caplog):
    host.after()
    host.clock.awake = False
    for _ in range(25):
        _advance(host, 60)
        assert getattr(host, entry)() == []
    host.clock.awake = True
    for _ in range(9):
        _advance(host, 60)
        assert getattr(host, entry)() == []
    _advance(host, 60)
    assert getattr(host, entry)() == (["default"] if entry == "after" else ["t_alarm"])
    text = caplog.text if entry == "after" else host.calls[0]
    assert "excluded 1500 s not fully awake" in text


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


@pytest.mark.parametrize("entry", ["after", "check"])
def test_mixed_awake_darkwake_restart_keeps_budget(host, entry):
    from hermes_cli import kanban_sync_outbox_observation as obs
    import json
    host.after()
    _advance(host, 300)
    getattr(host, entry)()
    host.clock.awake = False
    for _ in range(10):
        _advance(host, 60)
        assert getattr(host, entry)() == []
    record = json.loads(obs._path(host.path).read_text())
    assert record["excluded"] == 600
    alarm.reset()
    _advance(host, 60)
    assert host.after() == []
    record = json.loads(obs._path(host.path).read_text())
    assert record["excluded"] == 660
    host.clock.awake = True
    _advance(host, 299)
    assert getattr(host, entry)() == []
    _advance(host, 60)
    assert getattr(host, entry)() == (["default"] if entry == "after" else ["t_alarm"])


def test_warned_incident_survives_long_darkwake_restart(host):
    from hermes_cli import kanban_sync_outbox_observation as obs
    host.after()
    _advance(host, 600)
    assert host.after() == ["default"]
    assert host.check() == ["t_alarm"]
    host.clock.awake = False
    for _ in range(15):
        _advance(host, 60)
        assert host.after() == []
    alarm.reset()
    _advance(host, 60)
    assert host.after() == []
    seen = obs._SEEN[str(host.path.resolve())]
    assert seen.excluded == 960
    assert seen.filed is True
    assert host.clock.mono - seen.since == 600


@pytest.mark.parametrize("entry", ["after", "check"])
def test_injected_probe_failure_reads_awake_once(host, entry):
    calls = []
    def broken():
        calls.append(True)
        raise OSError("probe failure")
    host.after()
    _advance(host, 600)
    kwargs = {"mono": lambda: host.clock.mono, "awake_fn": broken}
    if entry == "after":
        result = alarm.after_round([SimpleNamespace(board="default", error=None, behind=False)],
                                   [("default", host.path)], **kwargs)
    else:
        result = alarm.check(boards=[("default", host.path)], file_fn=lambda *a: "t_alarm", **kwargs)
    assert result == (["default"] if entry == "after" else ["t_alarm"])
    assert calls == [True]


def test_dispatcher_samples_once_and_shares_probe_with_replica(host, monkeypatch):
    calls, replica_samples = [], []
    def awake():
        calls.append(True)
        return False
    def replica(boards, *, now, awake_fn):
        replica_samples.append(awake_fn())
        return []
    monkeypatch.setattr(alarm.replica_age, "check", replica)
    assert alarm.check(boards=[("default", host.path), ("other", host.path)],
                       mono=lambda: host.clock.mono, awake_fn=awake) == []
    assert calls == [True]
    assert replica_samples == [False]


def test_alternating_entrypoints_exclude_interval_only_once(host):
    from hermes_cli import kanban_sync_outbox_observation as obs
    host.after()
    host.clock.awake = False
    for _ in range(15):
        _advance(host, 60)
        assert host.after() == []
        assert host.check() == []
    seen = obs._SEEN[str(host.path.resolve())]
    assert seen.excluded == 900
    assert host.clock.mono - seen.since == 0


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
