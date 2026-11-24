"""Independent reviewer parity probes for PR 4292."""
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_governors_cli as gcli
from hermes_cli import kanban_spend_budget as sb
from tests.hermes_cli import test_kanban_governors as _gov
from tests.hermes_cli import test_kanban_spend_budget as _spend_tests

board = _gov.board
_tick = _gov._tick
_usage = _gov._usage
_run = _spend_tests._run


def test_spared_publisher_not_reported_spend_stopped(board, monkeypatch):
    _usage(monkeypatch, [10.0])
    monkeypatch.setattr(sb, 'limits', lambda: sb.Limits(runs=2, cache_read_tokens=0))
    url = 'https://github.com/softmax-universe/daveey-hermes/pull/4292'
    # Stub the GitHub boundary only; ownership, spending and sparing are real.
    monkeypatch.setattr(prs, 'pr_details', lambda value: ('OPEN', 'main'))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='published owner', assignee='default')
        _run(conn, tid, metadata={'published_pr': url})
        _run(conn, tid)
        assert sb.hold(conn, tid) is None
        assert any(e.kind == 'spend_budget_spared' for e in kb.list_events(conn, tid))
        answer = gcli.why(conn, tid, live=True)
        spend = next(s for s in answer['chain'] if s['governor'] == 'spend_budget')
        assert spend['verdict'] != 'stop', spend


def test_unreached_global_gates_not_reported_pass(board, monkeypatch):
    _usage(monkeypatch, [10.0])
    monkeypatch.setattr(kbd, '_memory_pressure_level', lambda sample=None: 'critical')
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='early cap', assignee='default')
        _tick(conn, max_spawn=0, max_in_progress=0)
        answer = gcli.why(conn, tid, live=True)
        steps = {s['governor']: s for s in answer['chain']}
        assert steps['max_spawn']['verdict'] == 'stop'
        assert steps['host_slots']['verdict'] != 'pass', steps['host_slots']
        assert steps['memory_pressure']['verdict'] != 'pass', steps['memory_pressure']


def test_over_budget_without_publication_still_stops(board, monkeypatch):
    """The exemption is not a blanket pass: no open owned PR still stops on the raw limit."""
    _usage(monkeypatch, [10.0])
    monkeypatch.setattr(sb, 'limits', lambda: sb.Limits(runs=2, cache_read_tokens=0))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='no publication', assignee='default')
        _run(conn, tid)
        _run(conn, tid)
        answer = gcli.why(conn, tid, live=True)
        spend = next(s for s in answer['chain'] if s['governor'] == 'spend_budget')
        assert spend['verdict'] == 'stop', spend
        assert spend['detail']['limit'] == 'runs'


def test_unmeasured_memory_is_unknown(board, monkeypatch):
    """A pressure reading the classifier could not take is not relabeled ok/pass."""
    _usage(monkeypatch, [10.0])
    monkeypatch.setattr(kbd, '_memory_pressure_level', lambda sample=None: 'unknown')
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='unread pressure', assignee='default')
        step = next(s for s in gcli.why(conn, tid, live=True)['chain'] if s['governor'] == 'memory_pressure')
        assert step['verdict'] == 'unknown', step


def test_missing_host_census_is_unknown():
    from types import SimpleNamespace
    from hermes_cli.kanban_governors_explain import Environment, _host_slots_answer

    env = Environment(board='default', profile_exists=None, default_assignee=None, per_profile_cap=None,
                      max_spawn=None, max_in_progress=2, review_enabled=False)
    answer = _host_slots_answer(env, SimpleNamespace(at_capacity=None), SimpleNamespace(host={}), 0, None)
    assert answer['verdict'] == 'unknown', answer
