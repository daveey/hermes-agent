"""Creation identity and distinct-owner declarations survive courier placement."""
import json

import pytest

from hermes_cli import kanban_placement as kp
from tests.hermes_cli import test_kanban_placement as helpers

mac = helpers.mac
IDENTITY = {
    "idempotency_key": "original:create:identity",
    "distinct_from": ["t_0123456789abcdef", "t_abcdef0123456789"],
    "distinct_reason": "Serializer prevention, not immediate receipt repair.",
}


def test_carried_identity_is_exact_json_data():
    assert kp.carried_options(IDENTITY) == IDENTITY
    assert kp.carried_options({"distinct_from": [], "distinct_reason": None}) == {}


@pytest.mark.parametrize("extra", [
    {"distinct_from": "t_0123456789abcdef"},
    {"distinct_from": ["not-a-task"]},
    {"distinct_from": [42]},
    {"distinct_from": ["t_0123456789abcdef"] * 33},
    {"distinct_from": False},
    {"distinct_reason": 42},
    {"distinct_reason": " "},
    {"distinct_reason": "x" * 501},
    {"idempotency_key": 42},
    {"idempotency_key": False},
    {"idempotency_key": " "},
    {"idempotency_key": "x" * 501},
    {"distinct_from": []},
    {"distinct_reason": None},
])
def test_invalid_identity_is_not_coerced_or_dropped(extra):
    with pytest.raises(ValueError):
        kp.carried_options({**IDENTITY, **extra})


@pytest.mark.parametrize("field", list(IDENTITY))
def test_legacy_client_never_drops_identity(mac, field):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"title": "Separate serializer work", "placement": "hub",
                                        field: IDENTITY[field]}))
    assert not out.get("ok"), out
    assert out["code"] == "placement_unavailable"
    assert mac["calls"] == [] and helpers._local_count() == 0


@pytest.mark.parametrize("session", [None, "20261027_183000_abc123"])
@pytest.mark.parametrize("target", ["hub", "macbook"])
def test_full_tool_envelope_retains_identity(mac, monkeypatch, session, target):
    from tools import kanban_tools as kt
    if target == "macbook":
        monkeypatch.setattr(kp.reg, "self_gateway", lambda: "hub")
        monkeypatch.setattr(kp, "target_live", lambda *a, **kw: None)
        monkeypatch.setattr(kp, "target_info", lambda *a, **kw: helpers.SILENT(target))
    mac["result"]["help"] = helpers.NEW_HELP
    out = json.loads(kt._handle_create({"title": "Separate serializer work", "placement": target,
                                        "session_id": session, **IDENTITY}))
    assert out["ok"] and out["placed"]["gateway"] == target, out
    assert helpers._options(mac["calls"][0]) == IDENTITY
    assert helpers._local_count() == 0


def test_explicit_key_retry_revalidates_envelope_without_new_identity(mac, monkeypatch):
    """A stored transport ID alone cannot establish that the caller's payload is identical."""
    from tools import kanban_tools as kt
    mac["result"]["help"] = helpers.NEW_HELP
    stored = {}

    def courier(argv):
        rid = argv[argv.index("--id") + 1]
        envelope = list(argv)
        if rid in stored and stored[rid] != envelope:
            return 2, "", "request_id_conflict: different envelope"
        stored[rid] = envelope
        return 0, rid, ""

    monkeypatch.setattr(kp, "hub_carries_options", lambda cli: True)
    monkeypatch.setattr(kp, "hub_runner", courier)
    monkeypatch.setattr(kp, "remote_state", lambda rid: "sent" if rid in stored else None)
    args = {"title": "Separate serializer work", **IDENTITY}
    first = json.loads(kt._handle_create(args))
    again = json.loads(kt._handle_create(args))
    assert first["ok"] and again["ok"]
    assert first["placed"]["request_id"] == again["placed"]["request_id"]
    assert again["placed"]["retry"] is True
    conflict = json.loads(kt._handle_create({**args, "distinct_reason": "Changed scope"}))
    assert not conflict.get("ok"), conflict
    assert "request_id_conflict" in conflict["error"]
    assert len(stored) == 1 and helpers._local_count() == 0


def test_direct_create_refuses_legacy_safety_metadata(monkeypatch):
    monkeypatch.setattr(kp, "hub_carries_options", lambda cli: False)
    monkeypatch.setattr(kp, "hub_runner", lambda argv: pytest.fail("must not queue"))
    out = kp.create_remote(IDENTITY, target="hub", self_gw="macbook", origin_session=None,
                           origin_profile="default", cfg={})
    assert not out["ok"]


def test_distinct_scope_participates_in_implicit_request_identity():
    args = {"title": "Separate serializer work", **IDENTITY}
    args.pop("idempotency_key")
    before = kp.request_id("macbook", args, "", kp.carried_options(args))
    changed = {**args, "distinct_reason": "Different purpose"}
    assert before != kp.request_id("macbook", changed, "", kp.carried_options(changed))
    assert before == kp.request_id("macbook", args, "", kp.carried_options(args))


def test_invalid_identity_refuses_auto_placement_without_local_fallback(mac):
    from tools import kanban_tools as kt
    mac["result"]["help"] = helpers.NEW_HELP
    out = json.loads(kt._handle_create({"title": "Separate work", **IDENTITY, "distinct_from": "bad"}))
    assert not out.get("ok") and "create_option_invalid" in out["error"], out
    assert mac["calls"] == [] and helpers._local_count() == 0


@pytest.mark.parametrize("prior", [None, "sent", "done"])
def test_explicit_key_offline_retry_keeps_prior_outcome(monkeypatch, prior):
    monkeypatch.setattr(kp, "hub_carries_options", lambda cli: True)
    monkeypatch.setattr(kp, "hub_runner", lambda argv: (3, "", "offline"))
    monkeypatch.setattr(kp, "remote_state", lambda rid: prior)
    out = kp.create_remote(IDENTITY, target="hub", self_gw="macbook", origin_session=None,
                           origin_profile="default", cfg={})
    assert out["ok"] and out["retry"] == (prior is not None)
    assert out["queued_only"] == (prior != "done")


def test_legacy_auto_create_keeps_explicit_key_locally(mac):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db_connect as kbc
    args = {"title": "Separate work", "idempotency_key": IDENTITY["idempotency_key"]}
    out = json.loads(kt._handle_create(args))
    assert out["ok"] and "placed" not in out and mac["calls"] == []
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT idempotency_key FROM tasks WHERE id=?", (out["task_id"],)).fetchone()[0] == args["idempotency_key"]


@pytest.mark.parametrize("modern", [False, True])
def test_forwarder_transport_identity_survives_pre_receipt_crash(mac, monkeypatch, modern):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        parent = helpers._done_parent(kb, conn)
        child = json.loads(kt._handle_create({"title": "Separate work", "parents": [parent]}))["task_id"]
        kb.recompute_ready(conn)
        forwarder = helpers._forwarder(conn, child, mac, monkeypatch)
        monkeypatch.setattr(kp, "hub_carries_options", lambda cli: modern)
        args = kpf.remote_args(conn, kb.get_task(conn, child), self_gw="macbook")
        key = f"placement-forward:macbook:{child}"
        sent = kp.create_remote(args, target="hub", self_gw="macbook", origin_session=None,
                                origin_profile="default", cfg={}, transport_key=key)
        assert sent["request_id"] == kp.request_id("macbook", {**args, "idempotency_key": key}, "")
        assert "idempotency_key" not in helpers._options(mac["calls"][0])
        # The courier accepted the request before FORWARDED was recorded. A parent edit
        # must not make recovery execute a duplicate locally or enqueue a second envelope.
        monkeypatch.setattr(kpf, "remote_args", lambda *a, **kw: {**args, "body": "Changed parent handoff"})
        assert forwarder.run() == "parked"
        assert kb.get_task(conn, child).status == "scheduled"
    assert len(mac["calls"]) == 1
