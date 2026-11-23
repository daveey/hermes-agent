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
