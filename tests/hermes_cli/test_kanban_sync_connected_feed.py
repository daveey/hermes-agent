"""Connected feed generations replace unchanged fallback pulls, not delivery."""
from pathlib import Path
from types import SimpleNamespace
import threading
import pytest

from hermes_cli import kanban_sync_client as client


@pytest.fixture(autouse=True)
def _boards_in_tmp_cwd(tmp_path, monkeypatch):
    """Board paths here are relative names: keep their SQLite files out of the checkout."""
    monkeypatch.chdir(tmp_path)


def test_real_feed_publishes_connected_and_disconnected_state():
    from websockets.sync.server import serve
    close_socket = threading.Event()
    connected = threading.Event()
    disconnected = threading.Event()
    stop = threading.Event()
    states = []
    changes = []

    def handler(ws):
        if close_socket.wait(10):
            ws.close()

    def state(board, live):
        states.append((board, live))
        if live:
            connected.set()
        else:
            stop.set()
            disconnected.set()

    with serve(handler, '127.0.0.1', 0) as server:
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        ident = client.Identity(f'http://127.0.0.1:{server.socket.getsockname()[1]}', 'test', 'gateway')
        watcher = client.FeedWatcher(ident, 'one', threading.Event(), lambda: not stop.is_set(),
                                     on_change=changes.append, on_state=state)
        thread = threading.Thread(target=watcher.run, daemon=True)
        thread.start()
        try:
            assert connected.wait(5)
            close_socket.set()
            assert disconnected.wait(5)
            assert states == [('one', True), ('one', False)]
            assert changes == ['one']
        finally:
            stop.set()
            close_socket.set()
            server.shutdown()
            thread.join(5)
            server_thread.join(5)
        assert not thread.is_alive()


def test_failed_pull_retries_without_new_feed_and_success_clears_retry():
    from hermes_cli.kanban_sync_work import SyncWork
    work = SyncWork(5)
    boards = [('one', Path('one')), ('two', Path('two'))]
    for slug, _ in boards:
        work.feed_state(slug, True)
    assert work.select(boards, 100, lambda _: False) == boards
    # Model a failed GET after the feed event was consumed, no further events.
    getattr(work, 'finished', lambda *args: None)('one', False)
    assert work.select(boards, 101, lambda _: False) == []
    assert work.select(boards, 105, lambda _: False) == boards[:1]
    work.finished('one', True)
    assert work.select(boards, 110, lambda _: False) == []
    # The independent safety audit cannot be postponed by frequent events.
    work.changed('one')
    assert work.select(boards, 159, lambda _: False) == boards[:1]
    assert work.select(boards, 160, lambda _: False) == boards


@pytest.mark.parametrize('fail_first', [False, True])
def test_connected_feed_skips_unchanged_poll_and_observes_next_change(monkeypatch, fail_first):
    boards = [(s, Path(s)) for s in ('connected', 'offline')]
    clock = [100.0]
    rounds = [[]]
    callbacks = {}
    pending = set()
    monkeypatch.setattr(client, 'load_identity', lambda: client.Identity('https://example.com', 'test', 'gateway'))
    monkeypatch.setattr(client, 'board_paths', lambda: boards)
    monkeypatch.setattr(client.sync, 'set_lease_client', lambda _: None)
    monkeypatch.setattr(client, 'publish_pickup_if_changed', lambda _: None)
    monkeypatch.setattr(client, 'reconcile_links', lambda *args, **kwargs: None)
    monkeypatch.setattr(client, 'outbox_pending', lambda paths: any(p in pending for p in paths))
    monkeypatch.setattr(client, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda _: None))
    from hermes_cli import machine_metrics
    monkeypatch.setattr(machine_metrics, 'Reporter', lambda _: SimpleNamespace(report_due=lambda: None))

    class Watcher:
        def __init__(self, ident, board, wake, keep_running, **kwargs):
            callbacks[board] = kwargs
            if board == 'connected':
                kwargs.get('on_state', lambda *a: None)(board, True)
        def run(self):
            pass

    class Thread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            pass
        def is_alive(self):
            return True

    monkeypatch.setattr(client, 'threading', SimpleNamespace(Thread=Thread, Event=threading.Event))
    monkeypatch.setattr(client, 'FeedWatcher', Watcher)
    def sync_board(slug, *args):
        rounds[-1].append(slug)
        error = 'temporary GET failure' if fail_first and slug == 'connected' and len(rounds) == 1 else None
        return client.RoundResult(board=slug, mode='primary', error=error)
    monkeypatch.setattr(client, 'sync_board', sync_board)
    def wait(*args):
        n = len(rounds)
        clock[0] = [105, 106, 107, 110, 111, 112, 160][n-1]
        if n == 2:
            callbacks['connected']['on_change']('connected')
        if n == 3:
            pending.add(Path('connected'))
        if n == 4:
            pending.clear()
            callbacks['connected'].get('on_state', lambda *a: None)('connected', False)
        if n == 5:
            callbacks['connected'].get('on_state', lambda *a: None)('connected', True)
            callbacks['connected']['on_change']('connected')
        rounds.append([])
        return 'period'
    monkeypatch.setattr(client, 'wait_for_work', wait)
    client.run_forever(lambda: len(rounds) < 8, interval=5)
    assert rounds[:7] == [
        ['connected', 'offline'], ['connected', 'offline'] if fail_first else ['offline'], ['connected'], ['connected'],
        ['connected', 'offline'], ['connected'], [],
    ]
