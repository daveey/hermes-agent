"""Distinct v2 pre-freeze and pre-POST custody cuts (not transport proof)."""
import pytest

from hermes_cli import kanban_release_final as final, kanban_release_prepared as prepared
from hermes_cli import kanban_release_home as home
from tests.hermes_cli import test_owner_release_accounting_v2 as helpers

board = helpers.board
proof = helpers.proof
assembly = helpers.assembly


@pytest.mark.parametrize('capability', [{}, {'owner_release_application': 1}])
def test_prepared_v2_without_wire_refuses_before_freeze(assembly, monkeypatch, capability):
    a = assembly
    row = helpers._setup(a, monkeypatch)
    proposal = prepared.prepare(a.conn, a.proof, delivery_uid='delivery-1', application_version=2)
    final.install(a.conn)
    assert final.saved(a.conn, 'intent-1') is None
    hub = helpers._V2Hub(a, capability)
    with pytest.raises(ValueError, match='application v2'):
        home._process(a.conn, hub, row, 'home', 'example')
    assert hub.cap_calls == 1 and hub.posts == []
    assert final.saved(a.conn, 'intent-1') is None
    assert prepared.verified_prepared(a.conn, 'intent-1') == proposal


def test_capability_loss_after_new_freeze_never_posts_and_retry_keeps_wire(assembly, monkeypatch):
    a = assembly
    row = helpers._setup(a, monkeypatch)
    hub = helpers._V2Hub(a, {})
    replies = iter([{'owner_release_application': 2}, {}])
    checks = []
    def capability(board):
        checks.append(board)
        return next(replies)
    monkeypatch.setattr(hub, 'decision_retain_capability', capability)
    with pytest.raises(ValueError, match='application v2'):
        home._process(a.conn, hub, row, 'home', 'example')
    assert checks == ['example', 'example'] and hub.posts == []
    wire = final.saved(a.conn, 'intent-1')
    assert wire['proposal']['version'] == 2
    monkeypatch.setattr(hub, 'decision_retain_capability', lambda board: {'owner_release_application': 2})
    got = home._process(a.conn, hub, row, 'home', 'example')
    assert got['result']['outcome'] == 'released'
    assert hub.posts == [[wire]] and final.saved(a.conn, 'intent-1') == wire
