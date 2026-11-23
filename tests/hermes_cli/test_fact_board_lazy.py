"""Board scopes construct no owner until scoped demand; activation is off-thread. [daveey]"""
import threading
from types import SimpleNamespace

import pytest

from hermes_cli import fact_board_startup as startup
from hermes_cli.fact_contract import Pending, Unavailable

BOARDS = tuple(f'b{i}' for i in range(11))


class FakeOwner:
    started, built = [], []

    def __init__(self, scope, capture, render, views, *, paths=(), bindings=()):
        FakeOwner.built.append(scope.board_id)
        self.scope, self.handle, self.closed = scope, None, False

    def start(self):
        FakeOwner.started.append(self.scope.board_id)
        self.handle = SimpleNamespace(v1=object(), demand=lambda *a: True)
        return self.handle

    def close(self):
        self.closed = True


def _api():
    views = SimpleNamespace(_installed=None, board_selector=dict)
    def bind(v1, scope, **kw):
        return SimpleNamespace(close=lambda: None, on_mode=lambda *a: None, prepare=lambda: True, scope=scope)
    views.bind = bind
    views.install_provider = lambda owner, provider: setattr(views, '_installed', (owner, provider))
    return SimpleNamespace(board_prewarm=SimpleNamespace(stop_board_prewarm=lambda: None), board_fact_views=views)


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    from hermes_cli import kanban_db
    FakeOwner.started, FakeOwner.built = [], []
    submitted = []
    from hermes_cli import fact_process
    monkeypatch.setattr(fact_process, 'submit', lambda key, callback: submitted.append((key, callback)))
    monkeypatch.setattr(startup, 'ViewOwner', FakeOwner)
    monkeypatch.setattr(kanban_db, 'kanban_db_path', lambda board=None: tmp_path / str(board) / 'kanban.db')
    monkeypatch.setattr(kanban_db, 'get_current_board', lambda: 'b0')
    rt = startup.BoardRuntime(_api(), boards=BOARDS)
    rt.submitted = submitted
    return rt


def test_eleven_boards_construct_nothing_until_demand(runtime):
    runtime.start()
    assert FakeOwner.built == [] and FakeOwner.started == []
    assert runtime.owners == {} and runtime.views == {} and runtime.submitted == []
    assert isinstance(runtime.provider('b0'), Pending)  # current board is lazy too
    assert FakeOwner.built == []  # request thread only enqueued
    runtime.submitted[0][1]()
    assert FakeOwner.built == ['b0'] and set(runtime.owners) == {'b0'}
    assert runtime.provider('b0') is runtime.views['b0']


def test_provider_only_enqueues_then_activates_once(runtime):
    runtime.start()
    assert isinstance(runtime.provider('b5'), Pending)
    assert isinstance(runtime.provider('b5'), Pending)
    assert FakeOwner.started == [] and FakeOwner.built == []  # request side did no start
    assert {key for key, _ in runtime.submitted} == {('board', runtime.home, runtime.profile, 'b5')}
    runtime.submitted[0][1]()
    runtime.submitted[1][1]()  # duplicate callback is idempotent
    assert FakeOwner.started == ['b5'] and FakeOwner.built == ['b5']
    assert runtime.provider('b5') is runtime.views['b5']
    assert isinstance(runtime.provider('unknown'), Unavailable)


def test_close_fences_pending_activation_and_closes_dormant(runtime):
    runtime.start()
    runtime.provider('b3')
    callback = runtime.submitted[0][1]
    runtime.close()
    callback()
    assert FakeOwner.built == [] and 'b3' not in runtime.views and runtime.owners == {}
    assert isinstance(runtime.provider('b3'), Unavailable)


def test_activation_blocks_close_until_complete(runtime, monkeypatch):
    runtime.start()
    runtime.provider('b2')
    callback = runtime.submitted[0][1]
    gate, entered = threading.Event(), threading.Event()
    original = FakeOwner.start
    def slow(self):
        entered.set()
        gate.wait(5)
        return original(self)
    monkeypatch.setattr(FakeOwner, 'start', slow)
    worker = threading.Thread(target=callback)
    worker.start()
    assert entered.wait(5)
    closer = threading.Thread(target=runtime.close)
    closer.start()
    closer.join(0.2)
    assert closer.is_alive()  # close waits behind the fence
    gate.set()
    worker.join(5), closer.join(5)
    assert runtime.owners['b2'].closed  # started owner was still drained


def test_failed_activation_is_unavailable_without_retry_and_drained(runtime, monkeypatch):
    runtime.start()
    runtime.provider('b4')
    monkeypatch.setattr(FakeOwner, 'start', lambda self: (_ for _ in ()).throw(OSError('x')))
    with pytest.raises(OSError):
        runtime.submitted[0][1]()  # fact_process logs and contains this
    assert isinstance(runtime.provider('b4'), Unavailable)
    assert len(runtime.submitted) == 1  # no retry
    owner = runtime.owners['b4']  # partial owner custody retained
    runtime.close()
    assert owner.closed


def test_failed_view_bind_retains_view_and_owner_for_close(runtime):
    runtime.start()
    closed = []
    def bind(v1, scope, **kw):
        return SimpleNamespace(close=lambda: closed.append(scope.board_id), scope=scope,
                               on_mode=lambda *a: (_ for _ in ()).throw(RuntimeError('mode')))
    runtime.api.board_fact_views.bind = bind
    runtime.provider('b6')
    with pytest.raises(RuntimeError):
        runtime.submitted[0][1]()
    assert 'b6' not in runtime.views and isinstance(runtime.provider('b6'), Unavailable)
    runtime.close()
    assert closed == ['b6'] and runtime.owners['b6'].closed


def test_unauthorized_board_never_enqueues(runtime):
    runtime.start()
    assert isinstance(runtime.provider('other'), Unavailable)
    assert runtime.submitted == []


def test_unscoped_and_ready_requests_are_current_and_named_demand_only(runtime):
    runtime.start()
    assert isinstance(runtime.provider(None), Unavailable)  # enqueue current; no catalog yet
    assert [key[-1] for key, _ in runtime.submitted] == ['b0']
    assert runtime.ready('b7') is False
    assert runtime.ready('nope') is False
    assert [key[-1] for key, _ in runtime.submitted] == ['b0', 'b7']
    assert FakeOwner.built == []
