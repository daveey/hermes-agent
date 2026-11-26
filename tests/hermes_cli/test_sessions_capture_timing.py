"""Opt-in capture timings observe, never drive, native sessions pacing."""
import json
import logging
from types import SimpleNamespace

from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_contract import Scope, SourceId
from hermes_cli.fact_store import FactStore
from hermes_cli.sessions_native_source import SessionsSource


def owner_at(monkeypatch, clock, *, enabled=True, count=1):
    from hermes_cli import sessions_native_source as module
    if enabled:
        monkeypatch.setenv('HERMES_SESSIONS_CAPTURE_TIMINGS', '1')
    else:
        monkeypatch.delenv('HERMES_SESSIONS_CAPTURE_TIMINGS', raising=False)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'time', lambda: clock[0])
    def reader(views):
        clock[0] += 2
        return [b'private-payload'] * len(views)
    owner = SessionsSource(FactStore(), SourceId(Scope('private-home', 'sessions'),
                           'sessions.display', 'test'), [], lambda *_: b'unexpected', capture_many=reader)
    owner._watch = SimpleNamespace(check=lambda: None)
    for index in range(count):
        owner.demand('sessions', {'id': 'private-selector', 'index': index})
    return owner


def timing_records(caplog):
    return [json.loads(record.getMessage()) for record in caplog.records
            if record.name == 'hermes_cli.sessions_capture_timing']


def test_opt_in_reports_actual_batch_without_private_data(monkeypatch, caplog):
    clock = [100.]
    with caplog.at_level(logging.INFO, logger='hermes_cli.sessions_capture_timing'):
        owner = owner_at(monkeypatch, clock, count=4)
        captured = owner.capture(None, CaptureBudget(max_seconds=5))
    records = timing_records(caplog)
    assert len(records) == 2, 'enabled header and one actual capture timing are required'
    assert records[0]['event'] == 'sessions.capture.enabled'
    record = records[1]
    assert record['event'] == 'sessions.capture.timing'
    assert record['seq'] == 1 and record['serial_first'] == 1 and record['serial_last'] == 4
    assert record['selected'] == 4 and record['failed'] == 0
    assert (record['started'], record['ended'], record['notbefore']) == (100., 102., 104.)
    assert captured.after == 4 and owner._next_capture == 104.
    assert record['pid'] > 0 and record['instance'] == records[0]['instance']
    assert not any(value in caplog.text for value in ('private-selector', 'private-home', 'private-payload'))


def test_default_off_has_no_records_and_same_capture_and_pacing(monkeypatch, caplog):
    clock = [100.]
    with caplog.at_level(logging.INFO, logger='hermes_cli.sessions_capture_timing'):
        off = owner_at(monkeypatch, clock, enabled=False)
        off_capture = off.capture(None, CaptureBudget(max_seconds=5))
        assert timing_records(caplog) == []
        clock[0] = 100.
        on = owner_at(monkeypatch, clock)
        on_capture = on.capture(None, CaptureBudget(max_seconds=5))
    assert off_capture == on_capture
    assert off._next_capture == on._next_capture == 104.
    assert off._serial == on._serial == 1


def test_noop_and_cooldown_do_not_emit_or_consume_sequence(monkeypatch, caplog):
    clock = [100.]
    with caplog.at_level(logging.INFO, logger='hermes_cli.sessions_capture_timing'):
        owner = owner_at(monkeypatch, clock, count=0)
        assert owner.capture(None, CaptureBudget()).fact is None
        owner.demand('sessions', {})
        owner.commit(owner.capture(None, CaptureBudget(max_seconds=5)))
        owner.invalidate()
        assert owner.capture(None, CaptureBudget()).fact is None
        clock[0] = 104.
        owner.commit(owner.capture(None, CaptureBudget(max_seconds=5)))
    records = timing_records(caplog)
    assert [record.get('seq') for record in records] == [None, 1, 2]
    assert [(r['serial_first'], r['serial_last']) for r in records[1:]] == [(1, 1), (2, 2)]
    assert records[2]['started'] >= records[1]['notbefore']


def test_transport_failure_is_recorded_without_changing_retry_or_facts(monkeypatch, caplog):
    clock = [100.]
    with caplog.at_level(logging.INFO, logger='hermes_cli.sessions_capture_timing'):
        owner = owner_at(monkeypatch, clock, count=4)
        def fail(_):
            clock[0] += 2
            raise RuntimeError('private-exception')
        owner.reader_many = fail
        capture = owner.capture(None, CaptureBudget(max_seconds=5))
    record = timing_records(caplog)[1]
    assert record['failed'] == record['selected'] == 4
    assert all(f.error_code == 'sessions_capture_failed' for f in capture.facts)
    assert set(owner._retry.values()) == {107.}
    assert owner._next_capture == 104.
    assert 'private-exception' not in caplog.text


def test_raising_logger_is_isolated_from_capture_state(monkeypatch):
    from hermes_cli import sessions_capture_timing as module
    clock = [100.]
    def broken(*_):
        raise OSError('logger unavailable')
    monkeypatch.setattr(module.logger, 'info', broken)
    owner = owner_at(monkeypatch, clock)
    capture = owner.capture(None, CaptureBudget(max_seconds=5))
    assert capture.after == 1 and capture.fact.health == 'fresh'
    assert owner._next_capture == 104.


def test_slow_logger_runs_outside_source_lock_after_pacing_is_fixed(monkeypatch):
    from hermes_cli import sessions_capture_timing as module
    clock = [100.]
    owner = owner_at(monkeypatch, clock)
    def slow(_):
        assert owner._lock.acquire(blocking=False), 'log handler must never hold source lock'
        owner._lock.release()
        assert owner._next_capture == 104.
        clock[0] += 20
    monkeypatch.setattr(module.logger, 'info', slow)
    capture = owner.capture(None, CaptureBudget(max_seconds=5))
    assert clock[0] == 122.
    assert (owner._next_capture, capture.next_deadline) == (104., 130.)


def test_flag_is_sampled_only_at_construction(monkeypatch, caplog):
    clock = [100.]
    with caplog.at_level(logging.INFO, logger='hermes_cli.sessions_capture_timing'):
        owner = owner_at(monkeypatch, clock)
        monkeypatch.setenv('HERMES_SESSIONS_CAPTURE_TIMINGS', '0')
        owner.capture(None, CaptureBudget(max_seconds=5))
        clock[0] = 100.
        disabled = owner_at(monkeypatch, clock, enabled=False)
        monkeypatch.setenv('HERMES_SESSIONS_CAPTURE_TIMINGS', '1')
        disabled.capture(None, CaptureBudget(max_seconds=5))
    assert len(timing_records(caplog)) == 2


def test_multiple_sources_in_same_process_have_distinct_instances(monkeypatch, caplog):
    clock = [100.]
    with caplog.at_level(logging.INFO, logger='hermes_cli.sessions_capture_timing'):
        owner_at(monkeypatch, clock)
        owner_at(monkeypatch, clock)
    records = timing_records(caplog)
    assert records[0]['pid'] == records[1]['pid']
    assert records[0]['instance'] != records[1]['instance']


def test_filtered_info_does_not_reconfigure_logging(monkeypatch, caplog):
    from hermes_cli import sessions_capture_timing as module
    clock = [100.]
    handlers = tuple(module.logger.handlers)
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        owner_at(monkeypatch, clock).capture(None, CaptureBudget(max_seconds=5))
        assert module.logger.level == logging.WARNING
        assert timing_records(caplog) == []
    assert tuple(module.logger.handlers) == handlers
