"""Local delivery still wakes after publication with main's original lock order. [daveey]"""
import threading

import pytest

from hermes_cli.fact_contract import Fact, FactKey, Scope
from hermes_cli.fact_inprocess import InProcessFacts, IncrementalLifecycle
from hermes_cli.fact_store import FactStore


@pytest.mark.parametrize('post_enqueue_notify', [False, True])
def test_quiet_publication_wakes_local_delivery(monkeypatch, post_enqueue_notify):
    store = FactStore()
    scope = Scope('review', 'default')
    owner = InProcessFacts(store, {scope}, demand=lambda *args: None)
    handle = owner.acquire(scope, lifecycle=IncrementalLifecycle({(scope, 'board')}))
    initial = threading.Event()
    delivered = threading.Event()

    def receive(delta):
        (delivered if delta.upserts else initial).set()

    sub = handle.v1.subscribe(scope, (), None, None, receive)
    try:
        assert initial.wait(3)
        store.publish([Fact(FactKey(scope, 'TaskFact', 't'), {'v': 1})])
        if post_enqueue_notify:
            with store._condition:
                store._condition.notify_all()
        assert delivered.wait(2), 'committed delta queued but local delivery slept through its only wake'
    finally:
        sub.close()
        handle.close()
        owner.close()
