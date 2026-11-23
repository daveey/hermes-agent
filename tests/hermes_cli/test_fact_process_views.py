"""Real view owners share scheduling while unused scopes stay inert. [daveey]"""
import threading
from hermes_cli.fact_contract import Scope
from hermes_cli.fact_view_owner import ViewOwner


def test_eleven_real_views_share_one_feed_and_store(tmp_path):
    owners = [ViewOwner(Scope(str(tmp_path), f'profile-{i}', 'default'),
                        lambda *_: None, lambda *_: {}, ('board',)) for i in range(11)]
    try:
        for owner in owners:
            owner.start()
        assert len({owner.owner.feed._thread for owner in owners}) == 1
        assert len({id(owner.store) for owner in owners}) == 1
        assert not any(t.name == 'fact-board-capture' for t in threading.enumerate())
    finally:
        for owner in owners:
            owner.close()


def test_real_view_captures_share_one_worker_and_keep_peer_progress(tmp_path):
    import time
    from hermes_cli.fact_board_inputs import CapturedInput
    from tests.waiting import wait_until
    entered, release, peer_done = threading.Event(), threading.Event(), threading.Event()
    def blocked(*_):
        entered.set()
        assert release.wait(5)
        return CapturedInput({}, (), time.time())
    def peer(*_):
        peer_done.set()
        assert release.wait(5)
        return CapturedInput({}, (), time.time())
    first = ViewOwner(Scope(str(tmp_path), 'first', 'default'), blocked, lambda *_: {}, ('board',))
    second = ViewOwner(Scope(str(tmp_path), 'peer', 'default'), peer, lambda *_: {}, ('board',))
    try:
        first.start().demand('board', {})
        assert entered.wait(5)
        second.start().demand('board', {})
        assert wait_until(lambda: second.source._worker is not None)
        assert not peer_done.is_set()
        assert sum(t.name == 'fact-board-capture' for t in threading.enumerate()) == 1
        release.set()
        assert peer_done.wait(5)
        first.close()
        assert second.owner.feed._thread.is_alive()
    finally:
        release.set()
        first.close()
        second.close()
