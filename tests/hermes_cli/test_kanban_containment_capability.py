"""Protocol advertisement must precede any canonical containment mutation."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_containment_remote as remote
from hermes_cli import kanban_sync_client as client
from hermes_cli.kanban_containment_lifecycle import ContainmentRefused

CAPABILITY = {
    "containment_protocol": 1,
    "receipt_envelopes": [1],
    "task_marker": "containment_episode",
    "resume_predicates": ["authority", "producer"],
}


@pytest.mark.parametrize(
    "field,value",
    [
        ("containment_protocol", None),
        ("containment_protocol", True),
        ("containment_protocol", 2),
        ("receipt_envelopes", None),
        ("receipt_envelopes", [True]),
        ("receipt_envelopes", [2]),
        ("task_marker", None),
        ("task_marker", "other"),
        ("resume_predicates", None),
        ("resume_predicates", ["authority"]),
    ],
)
def test_incompatible_advertisement_refuses(field, value):
    data = {**copy.deepcopy(CAPABILITY), field: value}
    hub = SimpleNamespace(containment_capability=lambda board: data)
    with pytest.raises(ContainmentRefused, match="incompatible"):
        remote.require_capability(hub, "fixture")


@pytest.mark.parametrize("data", [None, [], "unsupported"])
def test_nonmapping_advertisement_refuses(data):
    with pytest.raises(ContainmentRefused):
        remote.require_capability(
            SimpleNamespace(containment_capability=lambda board: data), "fixture"
        )


def test_exact_capability_and_real_client_route(monkeypatch):
    hub = client.Hub(client.Identity("http://127.0.0.1:1", "fixture-key", "fixture"))
    calls = []

    def called(*args):
        calls.append(args)
        return copy.deepcopy(CAPABILITY)

    monkeypatch.setattr(hub, "_call", called)
    try:
        remote.require_capability(hub, "fixture/encoded")
    finally:
        hub.close()
    assert calls == [("GET", "/v2/boards/fixture%2Fencoded/containment/capability")]


@pytest.mark.parametrize("failure", ["incompatible", "http", "os"])
def test_enrolled_capability_failure_closes_without_fallback(monkeypatch, failure):
    from hermes_cli import kanban_db_sync as sync

    closed = []

    def capability(board):
        assert board == "fixture"
        if failure == "http":
            raise client.HubError(404, "old Board")
        if failure == "os":
            raise OSError("offline fixture")
        return {**CAPABILITY, "containment_protocol": 0}

    hub = SimpleNamespace(
        ident=SimpleNamespace(gateway="fixture"),
        containment_capability=capability,
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(
        sync,
        "sync_state",
        lambda conn: {
            "mode": "primary",
            "board_id": "fixture",
            "gateway_id": "fixture",
        },
    )
    monkeypatch.setattr(client, "load_identity", lambda: None)
    monkeypatch.setattr(client, "Hub", lambda identity: hub)
    with pytest.raises((ContainmentRefused, client.HubError, OSError)):
        remote.enrolled(None)
    assert closed == [True]


def test_enrolled_exact_capability_keeps_connection(monkeypatch):
    from hermes_cli import kanban_db_sync as sync

    closed = []
    hub = SimpleNamespace(
        ident=SimpleNamespace(gateway="fixture"),
        containment_capability=lambda board: copy.deepcopy(CAPABILITY),
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(
        sync,
        "sync_state",
        lambda conn: {
            "mode": "primary",
            "board_id": "fixture",
            "gateway_id": "fixture",
        },
    )
    monkeypatch.setattr(client, "load_identity", lambda: None)
    monkeypatch.setattr(client, "Hub", lambda identity: hub)
    assert remote.enrolled(None) == (hub, "fixture")
    assert closed == []
