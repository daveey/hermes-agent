"""A Kanban worker that dies while constructing its agent names the cause on the card's crash record.

Before this, a ModelRoutingError at construction reached the card only as "Kanban execution unknown;
retained by owner"; the exception lived in managed-worker.log alone.
"""
import json

import pytest


def test_start_failure_books_a_redacted_one_line_cause(monkeypatch):
    from agent.model_routing import ModelRoutingError
    from gateway import session_kanban
    booked = []
    monkeypatch.setattr(session_kanban, '_record_worker_result',
                        lambda ctx, code, out, failure=None, extra=None, *, no_turn=False:
                        booked.append((code, out, no_turn)))
    frame = {'policy': {'kanban_json': json.dumps({'db': '/nonexistent', 'task_id': 't', 'run_id': 1})}}
    session_kanban.record_start_failure(frame, ModelRoutingError('Class selectors require provider auto;\nuse a concrete model'))
    (code, out, no_turn), = booked
    assert no_turn is True
    assert code == 1
    assert out == 'Worker could not start: ModelRoutingError: Class selectors require provider auto; use a concrete model'


def test_start_failure_line_redacts_secrets():
    from gateway.session_kanban import start_failure_line
    line = start_failure_line(RuntimeError('bad key sk-ant-api03-' + 'A' * 40))
    assert 'A' * 40 not in line and line.startswith('Worker could not start: RuntimeError:')


@pytest.mark.parametrize('cause, expected', [
    ('Worker could not start: ModelRoutingError: x',
     'Kanban execution unknown; retained by owner (Worker could not start: ModelRoutingError: x)'),
    ('', 'Kanban execution unknown; retained by owner'),
])
def test_unknown_receipt_quotes_the_booked_cause(monkeypatch, cause, expected):
    from hermes_cli import kanban_worker_client as client
    from hermes_cli.gateway_client import GatewayClientError
    monkeypatch.setattr('gateway.session_kanban.worker_result', lambda db, params: {'last_output': cause} if cause else {})
    with pytest.raises(GatewayClientError) as raised:
        client._exit_code({'status': 'unknown'}, {}, '/nonexistent')
    assert str(raised.value) == expected


def test_worker_failure_after_the_turn_books_its_cause(monkeypatch):
    """Run 10210: a class failover's sidecar write was refused, then the close-time
    ``flush_token_counts`` raised WorkerPersistenceError; the card booked ``last_output=''``."""
    from agent.runtime_session_store import WorkerPersistenceError
    from gateway import session_kanban
    booked = []
    monkeypatch.setattr(session_kanban, '_record_worker_result', lambda ctx, code, out, failure=None: booked.append((code, out)))
    frame = {'policy': {'kanban_json': json.dumps({'db': '/nonexistent', 'task_id': 't', 'run_id': 1})}}
    session_kanban.record_worker_failure(frame, WorkerPersistenceError('invalid_params'))
    assert booked == [(1, 'Worker failed: WorkerPersistenceError: invalid_params')]


def test_start_failure_is_not_booked_twice(monkeypatch):
    from agent.model_routing import ModelRoutingError
    from gateway import session_kanban
    booked = []
    monkeypatch.setattr(session_kanban, '_record_worker_result',
                        lambda ctx, code, out, failure=None, extra=None, *, no_turn=False:
                        booked.append((out, no_turn)))
    frame = {'policy': {'kanban_json': json.dumps({'db': '/nonexistent', 'task_id': 't', 'run_id': 1})}}
    exc = ModelRoutingError('boom')
    session_kanban.record_start_failure(frame, exc)
    session_kanban.record_worker_failure(frame, exc)
    assert booked == [('Worker could not start: ModelRoutingError: boom', True)]


def test_worker_failure_outside_kanban_books_nothing_and_never_raises(monkeypatch):
    from gateway import session_kanban
    monkeypatch.setattr(session_kanban, '_record_worker_result', lambda *a: (_ for _ in ()).throw(OSError('db gone')))
    session_kanban.record_worker_failure({'policy': {}}, RuntimeError('x'))  # not a Kanban attempt
    frame = {'policy': {'kanban_json': json.dumps({'db': '/nonexistent', 'task_id': 't', 'run_id': 1})}}
    session_kanban.record_worker_failure(frame, RuntimeError('x'))  # board write fails: swallowed


def test_managed_worker_main_books_the_cause_of_a_late_failure(monkeypatch):
    """Entry path: ``managed_worker.main`` catches the exception ``execute`` raised and books it."""
    import io
    from agent import managed_worker as mw
    from agent.runtime_session_store import WorkerPersistenceError
    from gateway import session_kanban
    frame = {'policy': {'kanban_json': json.dumps({'db': '/nonexistent', 'task_id': 't', 'run_id': 1})}}
    sent, booked = [], []

    class Channel:
        stream = io.BytesIO()

        def __init__(self, *_a):
            pass

        def send(self, kind, **payload):
            sent.append(kind)

    monkeypatch.setattr(mw, 'WorkerChannel', Channel)
    monkeypatch.setattr(mw.os, 'fdopen', lambda *a, **k: io.BytesIO())
    monkeypatch.setattr(mw.os, 'dup', lambda fd: fd)
    monkeypatch.setattr(mw.os, 'dup2', lambda a, b: None)
    monkeypatch.setattr(mw, 'hello', dict)
    monkeypatch.setattr(mw, 'read_frame', lambda stream: frame)
    monkeypatch.setattr(mw, 'validate_bootstrap', lambda f: f)
    # main() strictly parses its own argv (``--observation-fd`` only); under pytest sys.argv is pytest's.
    monkeypatch.setattr(mw.sys, 'argv', [mw.__file__])

    def execute(f, channel):
        raise WorkerPersistenceError('invalid_params')

    monkeypatch.setattr(mw, 'execute', execute)
    monkeypatch.setattr(session_kanban, '_record_worker_result', lambda ctx, code, out, failure=None: booked.append(out))
    assert mw.main() == 1
    assert booked == ['Worker failed: WorkerPersistenceError: invalid_params']
    assert sent[-1] == 'error'
