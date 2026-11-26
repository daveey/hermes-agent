"""Real subprocess CLI/socket emission; fake canonical adapter is not DO proof."""
import argparse
import io
import json

import pytest

from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_native_owner_release_cli as fixtures
from tools import origin_cli_bridge as bridge

turn = fixtures.turn
transport = fixtures.transport


def comment(body="latest owner instruction"):
    return {"uid": "comment-a", "task_id": "t_target", "author": "owner", "body": body, "created_at": 1,
            "seq": 1, "version": 1, "updated_at": 1, "hlc": "1", "origin_gateway": "foreign-home", "deleted_at": None}


def test_actual_cli_complete_large_inventory_flush_then_next_invocation_ack(turn, transport, monkeypatch):
    transport.comments = [comment("x" * 100000)]
    native = fixtures.launch(monkeypatch)
    try:
        first = fixtures.cli(native)
        assert first.returncode == 0, (first.stdout, first.stderr)
        value = json.loads(first.stdout)
        assert value["release_read"]["comments"] == transport.comments
        nonce = value["release_read"]["nonce"]
        with kbc.connect_closing() as conn:
            row = conn.execute("SELECT displayed_invocation,acknowledged FROM native_owner_release_reads").fetchone()
            assert row[0] is not None and row[1] == 0
        second = fixtures.cli(native, "--seen-release", nonce)
        assert second.returncode == 0, (second.stdout, second.stderr)
        assert json.loads(second.stdout)["release_stage"] == "acknowledged_pending_home"
        assert len(transport.sent) == 1
        with kbc.connect_closing() as conn:
            assert conn.execute("SELECT acknowledged FROM native_owner_release_reads").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='stop_released'").fetchone()[0] == 0
    finally:
        fixtures.close(native)


def test_socket_unconfirmed_token_cannot_ack_itself_and_private_receipt_single_use(turn, transport, monkeypatch):
    native = fixtures.launch(monkeypatch)
    args = {"board": "default", "task_id": "t_target", "episode": "episode-now", "capability_episodes": [], "runs": 1, "note": "original goal"}
    try:
        first = bridge.request(native.locator, args, operation="release")
        nonce = first["release_read"]["nonce"]
        second = bridge.request(native.locator, {**args, "seen_release": nonce}, operation="release")
        assert second["release_stage"] == "read_first_pending"
        assert second["release_read"]["nonce"] == nonce
        stale = bridge.request(native.locator, {"receipt": first["display_receipt"]}, operation="release_display")
        assert not stale["ok"]  # superseded invocation cannot confirm newer output
        reply = bridge.request(native.locator, {"receipt": second["display_receipt"]}, operation="release_display")
        assert reply == {"ok": True, "display_confirmed": True}
        assert not bridge.request(native.locator, {"receipt": second["display_receipt"]}, operation="release_display")["ok"]
        ack = bridge.request(native.locator, {**args, "seen_release": nonce}, operation="release")
        assert ack["release_stage"] == "acknowledged_pending_home"
    finally:
        fixtures.close(native)


def test_actual_cli_changed_comments_force_new_complete_display_before_ack(turn, transport, monkeypatch):
    native = fixtures.launch(monkeypatch)
    try:
        first = json.loads(fixtures.cli(native).stdout)
        old = first["release_read"]["nonce"]
        transport.comments = [comment()]
        second = fixtures.cli(native, "--seen-release", old)
        assert second.returncode == 0, (second.stdout, second.stderr)
        value = json.loads(second.stdout)
        assert value["release_stage"] == "read_first_pending"
        assert value["release_read"]["nonce"] != old
        assert value["release_read"]["comments"] == transport.comments
    finally:
        fixtures.close(native)


def test_cli_flush_failure_never_sends_display_confirmation(monkeypatch):
    from hermes_cli import kanban_cli_native as cli
    calls = []
    def request(locator, args, **kwargs):
        calls.append(kwargs["operation"])
        return {"ok": True, "task_id": "t_target", "release_stage": "read_first_pending",
                "release_read": {"nonce": "n", "comments": []}, "display_receipt": "r"}
    class Broken(io.StringIO):
        def flush(self):
            raise BrokenPipeError("fixture output failed")
    monkeypatch.setenv(bridge.ENV_KEY, "offered")
    monkeypatch.setattr(bridge, "request", request)
    monkeypatch.setattr(cli.sys, "stdout", Broken())
    args = argparse.Namespace(episode="episode", task_id="t_target", board="default", capability_episodes=[], runs=1, note="goal", json=True)
    with pytest.raises(BrokenPipeError):
        cli.offered_release(args)
    assert calls == ["release"]


@pytest.mark.parametrize("failure", ["expired", "foreign_launch", "revoked", "forged", "extra_fields", "not_selected"])
def test_display_receipt_failures_never_mark_emitted(turn, transport, monkeypatch, failure):
    import time
    import model_tools
    native = fixtures.launch(monkeypatch)
    other = fixtures.launch(monkeypatch) if failure == "foreign_launch" else None
    args = {"board": "default", "task_id": "t_target", "episode": "episode-now", "capability_episodes": [], "runs": 1, "note": "goal"}
    try:
        first = bridge.request(native.locator, args, operation="release")
        receipt = first["display_receipt"]
        if failure == "expired":
            native._displays[receipt] = (time.monotonic() - 121, native._displays[receipt][1])
        if failure == "revoked":
            turn.release()
        if failure == "not_selected":
            monkeypatch.setattr(model_tools, "_select_tool_names", lambda *a, **kw: set())
        packet = {"receipt": "forged" if failure == "forged" else receipt}
        if failure == "extra_fields":
            packet["intent_uid"] = first["intent_uid"]
        result = bridge.request((other or native).locator, packet, operation="release_display")
        assert not result["ok"]
        with kbc.connect_closing() as conn:
            assert conn.execute("SELECT displayed_invocation FROM native_owner_release_reads").fetchone()[0] is None
        native.retire()
        assert native._displays == {}
    finally:
        fixtures.close(native)
        if other:
            fixtures.close(other)
