"""The native links inventory is not a 2,000-key task transaction."""
import pytest

from hermes_cli import kanban_session_activity as activity
from hermes_cli.fact_links_startup import LinksRuntime
from hermes_cli.fact_remote_links import LinksUnavailable
from hermes_cli.kanban_links_refresh import ScopeRefresher
from tests.waiting import wait_until


@pytest.mark.parametrize('count', [4300, 9331])
def test_startup_captures_native_sized_links_inventory(monkeypatch, count):
    origin = ('home', 'default', 'boards', 'hub', 'mail')
    cards = [{'id': str(i), 'title': 'x' * 1400} for i in range(count)]
    monkeypatch.setattr(activity, '_origin_scope', lambda: origin)
    monkeypatch.setattr(activity, '_links_refresher', ScopeRefresher(lambda _: pytest.fail('legacy admission'), 16))
    monkeypatch.setattr(activity, '_project_remote_links', lambda: cards)
    runtime = LinksRuntime(origin).start()
    try:
        def captured():
            try:
                return runtime.binding.read(False) == cards
            except LinksUnavailable:
                return False
        wait_until(captured)
        assert origin in activity._links_refresher._fenced
    finally:
        runtime.close()


def test_links_budget_is_separate_and_bounded():
    from hermes_cli.fact_remote_links import LinksBudget
    from hermes_cli.fact_capture import CaptureBudget
    budget = LinksBudget()
    assert (budget.max_keys, budget.max_bytes, budget.max_seconds) == (16384, 16 * 1024 * 1024, 15)
    assert CaptureBudget().max_keys == 2000
    for kw in ({'max_keys': 16385}, {'max_bytes': 16 * 1024 * 1024 + 1}, {'max_seconds': 16},
               {'max_keys': 0}, {'max_bytes': 0}, {'max_seconds': 0}):
        with pytest.raises(ValueError):
            LinksBudget(**kw)


@pytest.mark.parametrize('case', ['six_seconds', 'time_overflow', 'rows_overflow', 'bytes_overflow'])
def test_links_envelope_acceptance_and_no_partial_publication(case, monkeypatch):
    from types import SimpleNamespace
    import time
    from hermes_cli import fact_remote_links as links
    from hermes_cli.fact_contract import Scope, SourceId
    from hermes_cli.fact_reducers import ReducerEngine
    from hermes_cli.fact_store import FactStore
    origin = ('home', 'default', 'boards', 'hub', 'mail')
    scope = Scope(*origin[:2])
    engine = ReducerEngine(FactStore(), scope)
    source = links.RemoteLinksSource(engine, SourceId(scope, 'remote.links', 'origin'), origin)
    clock = [0.0]
    monkeypatch.setattr(links, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(activity, '_origin_scope', lambda: origin)
    def project():
        clock[0] = 16 if case == 'time_overflow' else 6
        if case == 'rows_overflow':
            return [{}] * 16385
        if case == 'bytes_overflow':
            return [{'title': 'x' * (16 * 1024 * 1024)}]
        return [{'id': 'one'}]
    monkeypatch.setattr(activity, '_project_remote_links', project)
    source.watch(lambda: None)
    try:
        source._build(links.LinksBudget())
        capture = source._result
        assert capture.failed == (case != 'six_seconds')
        source.commit(capture)
        fact = engine.store.snapshot(scope).get(source.key)
        if case == 'six_seconds':
            assert fact.health == 'fresh' and len(fact.value['cards']) == 1
        else:
            assert fact.health == 'unknown' and not fact.value
            expected = {'time_overflow': 'links_capture_time_budget',
                        'rows_overflow': 'links_capture_row_budget',
                        'bytes_overflow': 'links_capture_byte_budget'}[case]
            assert capture.error_code == fact.error_code == expected
            binding = links.LinksFacts(SimpleNamespace(v1=SimpleNamespace(snapshot=lambda _: engine.store.snapshot(scope))),
                                       scope, origin)
            with pytest.raises(LinksUnavailable) as refused:
                binding.read(False)
            assert refused.value.error_code == expected
        assert 29 <= capture.next_deadline - clock[0] <= 30
    finally:
        source.close()
