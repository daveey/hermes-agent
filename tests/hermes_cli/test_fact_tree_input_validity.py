"""The project tree's captured input outlives a slow capture cycle (``fact_tree_startup``). [daveey]

Hub 2026-11-24, board facts on: four tree selectors, each walking twelve profiles, queued on the one shared capture
worker with the board views; in the loaded dashboard backend a cycle took longer than the 60 s input validity, the
inputs expired before their recapture and the projects tree answered 503 dependency_unavailable.
"""
import time

from hermes_cli.fact_board_inputs import BoardCapture
from hermes_cli.fact_contract import Scope, freeze
from hermes_cli.fact_tree_startup import TreeRuntime
from hermes_cli.fact_view_owner import ViewOwner


def _published(source, view, selector, observed_at):
    key = source.input_key(view, selector)
    source.commit(BoardCapture(key, freeze({'rows': []}), (), observed_at))
    return source.engine.store.snapshot(source.engine.scope).get(key)


def test_a_tree_input_stays_servable_for_five_minutes():
    runtime = TreeRuntime(targets=())
    now = time.time()
    fact = _published(runtime.owner.source, 'tree', {'surface': 'native', 'profile': None, 'preview_limit': 3,
                                                     'session_limit': 2000}, now)
    assert fact.health == 'fresh'
    assert fact.valid_until - fact.observed_at == 300  # five minutes, not the board's 60 s
    assert runtime.owner.source._refresh == 30  # still refreshed every 30 s
    assert runtime.owner.source._capture_seconds == 60


def test_a_tree_input_captured_two_minutes_ago_is_still_valid():
    """Past the board's 60 s and inside the tree's 300 s: the slow cycle the hub saw (inputs aged 64-178 s)."""
    runtime = TreeRuntime(targets=())
    now = time.time()
    fact = _published(runtime.owner.source, 'tree', {'surface': 'native', 'profile': None, 'preview_limit': 2000,
                                                     'session_limit': 2000}, now - 120)
    assert fact.valid_until > now  # still within its validity: fact_expiry leaves it servable


def test_a_failed_tree_capture_still_fails_closed_at_once():
    runtime = TreeRuntime(targets=())
    source = runtime.owner.source
    selector = {'surface': 'native', 'profile': None, 'preview_limit': 3, 'session_limit': 2000}
    _published(source, 'tree', selector, time.time())
    key = source.input_key('tree', selector)
    source.commit(BoardCapture(key=key, failed=True, observed_at=time.time()))
    fact = source.engine.store.snapshot(source.engine.scope).get(key)
    assert fact.health == 'stale' and fact.error_code == 'board_capture_failed'


def test_the_board_input_keeps_its_one_minute_validity():
    owner = ViewOwner(Scope('installation', 'default', 'board'), lambda *a: None, lambda *a: None, ('board',))
    fact = _published(owner.source, 'board', {'n': 1}, time.time())
    assert fact.valid_until - fact.observed_at == 60
    assert owner.source._capture_seconds == 30
