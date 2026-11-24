"""Owner orchestration refuses local/network identity skew before writes."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db_connect as dbc
from hermes_cli import kanban_home_owner as owner
from hermes_cli.kanban_sync_client import Hub, Identity
from tests.hermes_cli import test_home_transfer_capture as support

board = support.board


def identity():
    return dict(task="t_1234567890abcdef",source="worker-a3",destination="hub",nonce="owner-test-0000001",expected_version=1)


def test_supplied_ack_dictionary_is_not_a_transport(board):
    with pytest.raises(ValueError, match="real Hub transport"):
        owner.source_step(board, {"phase":"materialized"}, "default", identity())


def test_owner_refuses_wrong_local_actor_before_network(board, monkeypatch):
    hub = Hub(Identity("http://127.0.0.1:1", "fixture-key", "worker-a3"))
    monkeypatch.setattr(hub, "home_transfer", lambda *a: pytest.fail("network before local identity gate"))
    with pytest.raises(ValueError, match="gateway/board mismatch"):
        owner.source_step(board, hub, "default", identity())


def test_owner_never_sends_inside_native_writer(board):
    hub = Hub(Identity("http://127.0.0.1:1", "fixture-key", "worker-a3"))
    with dbc.write_txn(board), pytest.raises(ValueError, match="outside a writer"):
        owner.source_step(board, hub, "default", identity())


@pytest.mark.parametrize("change", [{"nonce":"changed-0000001"},{"expected_version":True},{"task":"other"},{"phase":"unknown"},{"rows":None}])
def test_authenticated_wrong_scope_response_never_reaches_local_finalizer(monkeypatch, change):
    ident = identity()
    hub = Hub(Identity("http://127.0.0.1:1", "fixture-key", "worker-a3"))
    monkeypatch.setattr(hub, "home_transfer", lambda *a: {**ident,"phase":"committed","rows":[],**change})
    with pytest.raises(ValueError, match="identity/shape mismatch"):
        owner._advance(hub, "default", ident, "read")
