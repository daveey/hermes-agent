"""The observer remains independent of claim admission and persists its outbox."""
import json
from contextlib import closing
from hermes_cli.kanban_db_connect import connect

from hermes_cli import worker_load_observer as observer


def test_threshold_continuity_restart_and_single_alert(tmp_path):
    path = tmp_path / 'observer'
    published = []
    def publish(alert):
        published.append(alert)
        return 't_alert'
    for now in range(0, 600, 30):
        state = observer.tick(path, now=now, load_per_core=2.1, evidence={'processes': []}, publish=publish)
        assert not published
    state = observer.tick(path, now=600, load_per_core=2.1, evidence={'processes': []}, publish=publish)
    assert len(published) == 1
    assert state['published_id'] == 't_alert'
    for now in (630, 660, 690):
        observer.tick(path, now=now, load_per_core=3, evidence={}, publish=publish)
    assert len(published) == 1
    assert json.loads((path / 'observer.json').read_text())['published_id'] == 't_alert'


def test_gap_and_unreadable_samples_are_explicit_unknown(tmp_path):
    sent = []
    for now in (0, 30, 60, 600):
        state = observer.tick(tmp_path, now=now, load_per_core=3, evidence={}, publish=lambda a: sent.append(a))
    assert not sent
    assert state['coverage'] == 'unknown_gap'
    assert state['high_since'] == 600
    state = observer.tick(tmp_path, now=630, load_per_core=None, evidence={}, publish=lambda a: sent.append(a))
    assert state['coverage'] == 'unknown_sample'
    assert state['high_since'] is None
    assert not sent


def test_exact_threshold_and_clock_reversal_break_continuity(tmp_path):
    publish = lambda a: 't_alert'
    for now in range(0, 601, 30):
        state = observer.tick(tmp_path, now=now, load_per_core=2, evidence={}, publish=publish)
    assert state['high_since'] is None
    observer.tick(tmp_path, now=630, load_per_core=3, evidence={}, publish=publish)
    state = observer.tick(tmp_path, now=620, load_per_core=3, evidence={}, publish=publish)
    assert state['coverage'] == 'unknown_gap'
    assert state['high_since'] == 620


def test_failed_publish_retries_same_outbox_even_after_load_recovers(tmp_path):
    seen = []
    def unavailable(alert):
        seen.append(alert['key'])
        raise OSError('unavailable')
    for now in range(0, 631, 30):
        observer.tick(tmp_path, now=now, load_per_core=3, evidence={'processes': []}, publish=unavailable)
    assert len(seen) == 2 and seen[0] == seen[1]
    recovered = []
    state = observer.tick(tmp_path, now=660, load_per_core=0.5, evidence={},
                          publish=lambda a: recovered.append(a['key']) or 't_delivered')
    assert recovered == [seen[0]]
    assert state['pending'] is None
    observer.tick(tmp_path, now=690, load_per_core=0.5, evidence={}, publish=unavailable)
    assert len(seen) == 2


def test_crash_after_remote_write_retries_same_key(tmp_path):
    calls = []
    def crash(alert):
        calls.append(alert['key'])
        raise KeyboardInterrupt
    import pytest
    with pytest.raises(KeyboardInterrupt):
        for now in range(0, 601, 30):
            observer.tick(tmp_path, now=now, load_per_core=3, evidence={}, publish=crash)
    observer.tick(tmp_path, now=630, load_per_core=3, evidence={}, publish=lambda a: calls.append(a['key']) or 't_one')
    assert calls[0] == calls[1]


def test_real_publisher_deduplicates_including_archived(tmp_path, monkeypatch):
    from hermes_cli import worker_compute_capacity as capacity
    from hermes_cli import kanban_db as kb
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('toolsets: [terminal]\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(capacity, 'policy_root', lambda: home)
    monkeypatch.setattr('socket.gethostname', lambda: 'fixture-worker')
    alert = dict(key='worker-load:fixture', since=0, observed_at=600, load_per_core=3,
                 evidence={'processes': [{'pid': 12, 'comm': 'python', 'cpu_percent': 95, 'card': 't_owner'}]})
    task = observer.publish_alert(alert)
    assert observer.publish_alert(alert) == task
    with closing(connect(db_path=home / 'kanban.db')) as conn:
        stored = kb.get_task(conn, task)
        assert 't_owner' in stored.body
        assert 'fixture-worker' in stored.title
        kb.archive_task(conn, task)
    assert observer.publish_alert(alert) == task
    with closing(connect(db_path=home / 'kanban.db')) as conn:
        assert conn.execute('SELECT count(*) FROM tasks WHERE idempotency_key=?', (alert['key'],)).fetchone()[0] == 1


def test_main_records_failed_load_sample_as_unknown(tmp_path, monkeypatch):
    from hermes_cli import worker_compute_capacity as capacity
    from hermes_cli import worker_load_snapshot as snapshot
    monkeypatch.setattr(capacity, 'worker_enabled', lambda: True)
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path)
    monkeypatch.setattr(capacity, 'effective_cores', lambda: 16)
    def unavailable():
        raise OSError('unreadable')
    monkeypatch.setattr(observer.os, 'getloadavg', unavailable)
    monkeypatch.setattr(snapshot, 'snapshot', unavailable)
    observer.main()
    state = json.loads((tmp_path / 'worker-resources/observer/observer.json').read_text())
    assert state['coverage'] == 'unknown_sample'
    assert state['pending'] is None


def test_auxiliary_evidence_failure_does_not_suppress_valid_load(tmp_path, monkeypatch):
    from hermes_cli import worker_compute_capacity as capacity
    from hermes_cli import worker_load_snapshot as snapshot
    monkeypatch.setattr(capacity, 'worker_enabled', lambda: True)
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path)
    monkeypatch.setattr(capacity, 'effective_cores', lambda: 16)
    monkeypatch.setattr(observer.os, 'getloadavg', lambda: (48, 48, 48))
    def broken():
        raise TypeError('malformed process evidence')
    monkeypatch.setattr(snapshot, 'snapshot', broken)
    observer.main()
    state = json.loads((tmp_path / 'worker-resources/observer/observer.json').read_text())
    assert state['coverage'] == 'observed'
    assert state['load_per_core'] == 3
    assert state['high_since'] is not None


def test_concurrent_tick_cannot_erase_outbox(tmp_path):
    import fcntl
    import pytest
    observer.tick(tmp_path, now=0, load_per_core=3, evidence={}, publish=lambda a: 't_alert')
    path = tmp_path / 'observer.json'
    before = path.read_bytes()
    with (tmp_path / 'observer.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            observer.tick(tmp_path, now=30, load_per_core=0, evidence={}, publish=lambda a: 't_alert')
    assert path.read_bytes() == before
