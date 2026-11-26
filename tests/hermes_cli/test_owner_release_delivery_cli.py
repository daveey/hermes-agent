"""Real child CLI + native socket publication; fake canonical adapter, not DO/HOME proof."""
import json

import pytest

from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_native_owner_release_cli as fixtures

turn = fixtures.turn
transport = fixtures.transport


def test_real_cli_ack_publishes_frozen_carrier_then_reconciles_after_reply_loss(turn, transport, monkeypatch):
    native = fixtures.launch(monkeypatch)
    try:
        first = fixtures.cli(native)
        assert first.returncode == 0, first.stderr
        nonce = json.loads(first.stdout)["release_read"]["nonce"]
        transport.drop_carrier_reply = True
        lost = fixtures.cli(native, "--seen-release", nonce)
        assert lost.returncode == 2, (lost.stdout, lost.stderr)
        assert json.loads(lost.stdout)["outcome"] == "outcome_unknown"
        assert len(transport.carriers) == 1
        wire = transport.carriers[0]
        payload = json.loads(wire["fields"]["payload"])
        assert payload["intent"] == transport.sent[0]["intent"]
        assert payload["carrier"] == "owner_release"
        assert payload["seen_comments"] == [] and payload["nonce"] == nonce
        transport.support = 0
        def unavailable(*a, **kw):
            pytest.fail("pending delivery must reconcile before private mutable comments/proof")
        monkeypatch.setattr(transport, "owner_release_comments", unavailable)
        monkeypatch.setattr(transport, "owner_release_evidence", unavailable)
        replay = fixtures.cli(native)
        assert replay.returncode == 0, (replay.stdout, replay.stderr)
        value = json.loads(replay.stdout)
        assert value["delivery_uid"] == wire["uid"] and value["publication_stage"] == "published"
        assert value["release_stage"] == "acknowledged_pending_home"
        assert len(transport.carriers) == 1 and len(transport.sent) == 1
        with kbc.connect_closing() as conn:
            assert conn.execute("SELECT stage FROM native_owner_release_deliveries").fetchone()[0] == "published"
            assert conn.execute("SELECT count(*) FROM task_events WHERE kind='stop_released'").fetchone()[0] == 0
    finally:
        fixtures.close(native)


def test_real_cli_unconfirmed_nonce_never_publishes_carrier(turn, transport, monkeypatch):
    native = fixtures.launch(monkeypatch)
    try:
        from tools import origin_cli_bridge as bridge
        args = {"board": "default", "task_id": "t_target", "episode": "01a216f3-6633-7617-9d00-d3779607ae0b",
                "capability_episodes": ["01a21357-6500-7f4a-9891-c1bf7cdd4ca3"], "runs": 1,
                "note": "complete the exact original acceptance"}
        # Raw socket reply receives output but deliberately never confirms emission.
        first = bridge.request(native.locator, args, operation="release")
        nonce = first["release_read"]["nonce"]
        second = fixtures.cli(native, "--seen-release", nonce)
        assert second.returncode == 0, (second.stdout, second.stderr)
        assert json.loads(second.stdout)["release_stage"] == "read_first_pending"
        assert transport.carriers == []
    finally:
        fixtures.close(native)
