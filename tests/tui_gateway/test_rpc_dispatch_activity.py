"""Real ``tui_gateway.server`` JSON-RPC dispatch against the local work registry."""
import concurrent.futures
import threading

import pytest

from hermes_cli import local_work
from hermes_cli.local_work import Registry
from hermes_constants import hermes_home_key
from tests.waiting import wait_until
from tui_gateway import server
from tui_gateway.contracts import METHODS, activity


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reg = Registry(homes=[hermes_home_key(tmp_path)])
    monkeypatch.setattr(local_work, "_registry", reg)
    # Handlers here are test doubles; the classification, not param validation, is the subject.
    monkeypatch.setattr(server._contracts, "validate_params", lambda contract, params: (params, None))
    monkeypatch.setattr(server._contracts, "check_params_accepted", lambda *a: None)
    monkeypatch.setattr(server._contracts, "check_result", lambda *a: None)
    return reg


def _call(method):
    return server.handle_request({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}})


def test_every_passive_method_is_a_known_contract():
    assert activity.PASSIVE_METHODS <= set(METHODS), sorted(activity.PASSIVE_METHODS - set(METHODS))
    for work in ("prompt.submit", "llm.oneshot", "prompt.background", "slash.exec", "totally.unknown"):
        assert activity.is_work(work)
    assert activity.is_work(None) and activity.is_work("")


def test_inline_passive_unclassified_and_work_methods(registry, monkeypatch):
    seen = {}

    def probe(name):
        def handler(rid, params):
            seen[name] = registry.snapshot().active
            return {"jsonrpc": "2.0", "id": rid, "result": {}}
        return handler

    for name in ("session.list", "prompt.submit", "test.unclassified"):
        monkeypatch.setitem(server._methods, name, probe(name))
    before = registry.snapshot().epoch
    _call("session.list")
    passive_epoch = registry.snapshot().epoch
    _call("prompt.submit")
    _call("test.unclassified")
    assert seen == {"session.list": 0, "prompt.submit": 1, "test.unclassified": 1}
    assert passive_epoch == before  # passive polling leaves no mark at all
    assert registry.snapshot().active == 0 and registry.snapshot().unknowns == ()


def test_work_scope_released_when_inline_handler_raises(registry, monkeypatch):
    def boom(rid, params):
        raise ValueError("handler bug")
    monkeypatch.setitem(server._methods, "test.boom", boom)
    with pytest.raises(ValueError):
        _call("test.boom")
    assert registry.snapshot().active == 0


def test_pool_handler_reserves_before_submit_and_releases_at_worker_exit(registry, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    responses = []

    def slow(rid, params):
        entered.set()
        assert release.wait(10)
        return {"jsonrpc": "2.0", "id": rid, "result": {}}

    monkeypatch.setitem(server._methods, "llm.oneshot", slow)

    class Sink:
        def write(self, frame):
            responses.append(frame)
            return True

    seen_at_submit = []
    real_submit = server._pool.submit

    def spy(fn, *a, **k):
        seen_at_submit.append(registry.snapshot().active)
        return real_submit(fn, *a, **k)
    monkeypatch.setattr(server._pool, "submit", spy)
    assert server.dispatch({"jsonrpc": "2.0", "id": 9, "method": "llm.oneshot", "params": {}}, Sink()) is None
    assert seen_at_submit == [1]
    assert entered.wait(10) and registry.snapshot().active == 1
    release.set()
    wait_until(lambda: registry.snapshot().active == 0, what="pool handler release")
    wait_until(lambda: responses, what="response written")


def test_pool_work_canceled_before_start_releases_once(registry, monkeypatch):
    gate = threading.Event()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(server, "_pool", pool)
    blocker = pool.submit(gate.wait, 10)
    monkeypatch.setitem(server._methods, "llm.oneshot",
                        lambda rid, p: {"jsonrpc": "2.0", "id": rid, "result": {}})
    assert server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "llm.oneshot", "params": {}}, None) is None
    assert registry.snapshot().active == 1  # queued handler is accepted work
    pool.shutdown(wait=False, cancel_futures=True)  # cancels the queued future before it starts
    wait_until(lambda: registry.snapshot().active == 0, what="canceled release")
    gate.set()
    blocker.result(10)
    snap = registry.snapshot()
    assert snap.active == 0 and "unmatched-finish" not in snap.unknowns
