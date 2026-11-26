"""Actual CLI -> inherited socket -> native parent -> canonical API adapter.

The adapter is not a deployed Board proof. No legacy release or actor test seam
is substituted for native custody; the parent's durable turn fixture is real.
"""
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db_connect as kbc, kanban_release_intent as intent, kanban_sync_client as client
from tests.hermes_cli import test_origin_native_admission as fixtures
from tools import origin_cli_bridge as bridge
from tools.environments.local import build_subprocess_env

turn = fixtures.turn


@pytest.fixture
def transport(monkeypatch):
    class Hub:
        ident = SimpleNamespace(gateway="here")
        support = 1
        sent = []
        evidence = None
        proof_changes = {}
        calls = []
        carriers = []
        carrier_rows = {}
        drop_carrier_reply = False
        def __init__(self, *args):
            pass
        def close(self):
            pass
        def decision_retain_capability(self, board):
            self.calls.append(("capability", board))
            return {"owner_release": self.support}
        def row_evidence(self, board, table, uid, **kwargs):
            self.calls.append(("row", board, table, uid))
            if table == "task_events":
                row = self.carrier_rows.get(uid)
                return {"board": board, "table": table, "uid": uid, "state": "live" if row else "never_existed",
                        "row": row, "tombstone": None, "floor": 0}
            return {"state": "live", "row": {"id": uid, "created_on": "foreign-home"}}
        def ops(self, board, ops):
            if ops[0].get("table") == "task_events":
                wire = ops[0]
                with kbc.connect_closing() as conn:
                    saved = conn.execute("SELECT wire,stage FROM native_owner_release_deliveries WHERE delivery_uid=?",
                                         (wire["uid"],)).fetchone()
                    assert json.loads(saved[0]) == wire and saved[1] == "send_pending"
                    assert not conn.in_transaction
                self.carriers.append(wire)
                self.carrier_rows[wire["uid"]] = {**wire["fields"], "uid": wire["uid"], "origin_gateway": "here",
                                                 "hlc": wire["hlc"], "deleted_at": None, "seq": 1, "version": 1}
                if self.drop_carrier_reply:
                    raise OSError("carrier reply lost after commit")
                return {"results": [{"status": "applied"}]}
            with kbc.connect_closing() as conn:
                row = conn.execute("SELECT body, stage FROM native_owner_release_intents WHERE intent_uid=?", (ops[0]["intent"]["intent_uid"],)).fetchone()
                assert row[1] == "send_pending" and not conn.in_transaction
                assert json.loads(row[0]) == ops[0]["intent"]
            self.sent.extend(ops)
            return {"results": [{"status": "applied"}]}
        comments = []
        def owner_release_comments(self, board, uid):
            body = self.sent[-1]["intent"]
            return {"version": 1, "intent": body, "digest": intent.digest(body), "comments": self.comments}
        def owner_release_application(self, board, uid):
            from hermes_cli.kanban_sync_client import HubError
            raise HubError(404, "fixture not applied")
        def owner_release_evidence(self, board, uid):
            body = self.sent[-1]["intent"]
            return self.evidence or {"version": 1, "state": "authorized", "intent": body, "digest": intent.digest(body),
                                     **self.proof_changes}
    monkeypatch.setattr(client, "load_identity", lambda: Hub.ident)
    monkeypatch.setattr(client, "Hub", Hub)
    return Hub


def launch(monkeypatch, enabled=("terminal",), selected=("terminal",)):
    import model_tools
    with monkeypatch.context() as scoped:
        scoped.setattr(model_tools, "_handle_function_call", lambda *a: bridge.Launch("native-release"))
        result = model_tools.handle_function_call("terminal", {}, enabled_tools=list(enabled))
    monkeypatch.setattr(model_tools, "_select_tool_names", lambda *a, **kw: set(selected))
    return result


def cli(launch, *extra):
    env = build_subprocess_env(scrub_secrets=True)
    env["PYTHONPATH"] = os.getcwd()
    env[bridge.ENV_KEY] = launch.locator
    return subprocess.run([sys.executable, "-m", "hermes_cli.main", "kanban", "release", "t_target",
                           "--episode", "01a216f3-6633-7617-9d00-d3779607ae0b", "--runs", "1",
                           "--capability-episode", "01a21357-6500-7f4a-9891-c1bf7cdd4ca3",
                           "--note", "complete the exact original acceptance", "--json", *extra],
                          env=env, pass_fds=(launch.child_fd,), capture_output=True, text=True,
                          encoding="utf-8", timeout=25, check=False)


def close(launch):
    launch.close()
    launch._thread.join(2)
    assert not launch._thread.is_alive()


def test_actual_cli_freezes_native_actor_and_reports_pending_not_release(turn, transport, monkeypatch):
    native = launch(monkeypatch)
    try:
        result = cli(native)
        assert result.returncode == 0, (result.stdout, result.stderr)
        value = json.loads(result.stdout)
        assert value["release_stage"] == "read_first_pending" and value["ok"]
        assert "released" not in value and "created" not in value
        body = transport.sent[0]["intent"]
        assert body["actor"] == {"gateway": "here", "profile": "default", "session": turn.agent.session_id}
        assert body["home"] == "foreign-home"
        assert body["episode"] == "01a216f3-6633-7617-9d00-d3779607ae0b"
        with kbc.connect_closing() as conn:
            assert conn.execute("SELECT stage FROM native_owner_release_intents").fetchone()[0] == "read_first_pending"
            assert conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='stop_released'").fetchone()[0] == 0
        again = cli(native)
        assert again.returncode == 0, (again.stdout, again.stderr)
        assert json.loads(again.stdout) == value
        assert len(transport.sent) == 1
    finally:
        close(native)


@pytest.mark.parametrize("failure", ["revoked", "delegated", "no_grant", "not_selected", "unsupported"])
def test_actual_cli_fails_closed_before_any_journal_or_send(turn, transport, monkeypatch, failure):
    from agent.delegation_context import delegated_child_context
    from contextlib import nullcontext
    if failure == "unsupported":
        transport.support = 0
    with delegated_child_context() if failure == "delegated" else nullcontext():
        native = launch(monkeypatch, enabled=() if failure == "no_grant" else ("terminal",),
                        selected=() if failure == "not_selected" else ("terminal",))
    try:
        if failure == "revoked":
            turn.release()
        result = cli(native)
        assert result.returncode == 2, (result.stdout, result.stderr)
        assert not json.loads(result.stdout)["ok"]
        assert transport.sent == []
        with kbc.connect_closing() as conn:
            assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='native_owner_release_intents'").fetchone()
    finally:
        close(native)


@pytest.mark.parametrize("changes", [{"version": True}, {"version": 1.0}, {"state": "created"},
                                    {"digest": "forged"}, {"extra": "not-v1"}, {"intent": {}}])
def test_actual_cli_rejects_malformed_private_proof_without_reauthorizing(turn, transport, monkeypatch, changes):
    transport.proof_changes = changes
    native = launch(monkeypatch)
    try:
        result = cli(native)
        assert result.returncode == 2, (result.stdout, result.stderr)
        assert json.loads(result.stdout)["outcome"] == "outcome_unknown"
        with kbc.connect_closing() as conn:
            assert conn.execute("SELECT stage FROM native_owner_release_intents").fetchone()[0] == "send_pending"
        assert len(transport.sent) == 1
    finally:
        close(native)


@pytest.mark.parametrize("terminal", ["expired", "refused"])
def test_actual_cli_preserves_definitive_negative_and_replays_without_send(turn, transport, monkeypatch, terminal):
    transport.proof_changes = {"state": terminal}
    native = launch(monkeypatch)
    try:
        first = cli(native)
        assert first.returncode == 2
        value = json.loads(first.stdout)
        assert value["release_stage"] == terminal and not value["ok"]
        assert value["intent_uid"] == transport.sent[0]["intent"]["intent_uid"]
        again = cli(native)
        assert again.returncode == 2 and json.loads(again.stdout) == value
        assert len(transport.sent) == 1
    finally:
        close(native)


def test_actual_cli_explicit_expiry_ack_replaces_only_that_one_intent(turn, transport, monkeypatch):
    transport.proof_changes = {"state": "expired"}
    native = launch(monkeypatch)
    try:
        old_result = cli(native)
        old = json.loads(old_result.stdout)["intent_uid"]
        transport.proof_changes = {}
        fresh_result = cli(native, "--fresh-after-expiry", old)
        assert fresh_result.returncode == 0, (fresh_result.stdout, fresh_result.stderr)
        fresh = json.loads(fresh_result.stdout)
        assert fresh["intent_uid"] != old
        assert cli(native, "--fresh-after-expiry", old).stdout == fresh_result.stdout
        assert len(transport.sent) == 2
        transport.calls.clear()
        transport.support = 0
        def unavailable(*a, **kw):
            pytest.fail("recorded expiry-ACK successor must not re-resolve HOME")
        monkeypatch.setattr(transport, "row_evidence", unavailable)
        assert cli(native, "--fresh-after-expiry", old).stdout == fresh_result.stdout
        assert transport.calls == []
        assert len(transport.sent) == 2
        before, after = (op["intent"] for op in transport.sent)
        assert {k: v for k, v in before.items() if k != "intent_uid"} == {k: v for k, v in after.items() if k != "intent_uid"}
    finally:
        close(native)


def test_actual_cli_historical_journal_replay_precedes_mutable_preflight(turn, transport, monkeypatch):
    native = launch(monkeypatch)
    try:
        transport.proof_changes = {"state": "expired"}
        first = cli(native)
        assert first.returncode == 2
        before = json.loads(first.stdout)
        transport.calls.clear()
        transport.support = 0
        def unavailable(*a, **kw):
            pytest.fail("historical journal replay must not re-resolve mutable HOME")
        monkeypatch.setattr(transport, "row_evidence", unavailable)
        again = cli(native)
        assert again.returncode == 2
        assert json.loads(again.stdout) == before
        assert transport.calls == []
        assert len(transport.sent) == 1
    finally:
        close(native)


def test_actual_cli_pending_journal_reconcile_precedes_mutable_preflight(turn, transport, monkeypatch):
    native = launch(monkeypatch)
    try:
        first = cli(native)
        assert first.returncode == 0
        transport.calls.clear()
        transport.support = 0
        def unavailable(*a, **kw):
            pytest.fail("pending journal reconciliation must not re-resolve mutable HOME")
        monkeypatch.setattr(transport, "row_evidence", unavailable)
        again = cli(native)
        assert again.returncode == 0, (again.stdout, again.stderr)
        assert json.loads(again.stdout) == json.loads(first.stdout)
        assert transport.calls == []
        assert len(transport.sent) == 1
    finally:
        close(native)


def test_actual_cli_never_accepts_foreign_integer_seen(turn, transport, monkeypatch):
    native = launch(monkeypatch)
    try:
        result = cli(native, "--seen", "123")
        assert result.returncode == 2
        assert transport.calls == [] and transport.sent == []
    finally:
        close(native)
