"""Durable starts use the real session store, never live agents/credentials."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_state import SessionDB
from tui_gateway.session_start_requests import StartIntent, StartOwner, StartStore


@pytest.fixture
def db(tmp_path):
    store = SessionDB(tmp_path / "state.db")
    yield store
    store.close()


def owner(**overrides):
    return StartOwner(**{**dict(gateway_id="gateway-example", profile="default", principal="user-example"), **overrides})


def accept(db, **overrides):
    args = dict(owner=owner(), namespace="example", request_id="request-example",
                intent=StartIntent(title="Investigate example", text="Inspect only."))
    args.update(overrides)
    return StartStore(db).accept(**args)


def test_response_loss_and_restart_keep_one_session_and_first_turn(db):
    first = accept(db)
    second = accept(db)
    assert first == second
    reopened = SessionDB(db.db_path)
    try:
        assert accept(reopened) == first
        assert len(reopened.get_messages(first["stored_session_id"])) == 1
        assert reopened.get_session(first["stored_session_id"])["title"] == "Investigate example"
    finally:
        reopened.close()


def test_concurrent_handles_accept_once(db):
    def run(_):
        handle = SessionDB(db.db_path)
        try:
            return accept(handle)
        finally:
            handle.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(8)))
    assert all(result == results[0] for result in results)
    assert len(db.get_messages(results[0]["stored_session_id"])) == 1


@pytest.mark.parametrize("scope", [dict(profile="other"), dict(gateway_id="other"), dict(principal="other")])
def test_owner_scope_never_reuses_another_start(db, scope):
    assert accept(db)["stored_session_id"] != accept(db, owner=owner(**scope))["stored_session_id"]


def test_changed_payload_is_conflict(db):
    accept(db)
    with pytest.raises(ValueError, match="conflict"):
        accept(db, intent=StartIntent(title="Changed", text="Different"))


def test_crash_during_accept_rolls_back_all_three_rows(db, monkeypatch):
    store = StartStore(db)
    original = db._bump_session_counters
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated crash before commit")
    monkeypatch.setattr(db, "_bump_session_counters", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        accept(db)
    assert store.lookup(owner(), "example", "request-example") is None
    assert db._read_one("SELECT COUNT(*) FROM sessions")[0] == 0
    assert db._read_one("SELECT COUNT(*) FROM messages")[0] == 0
    monkeypatch.setattr(db, "_bump_session_counters", original)
    assert accept(db)["state"] == "queued"


def test_claim_never_replays_after_response_loss_or_restart(db):
    row = accept(db)
    store = StartStore(db)
    assert store.claim(row["first_turn_id"], "runtime-example")
    assert not store.claim(row["first_turn_id"], "runtime-example")
    assert not StartStore(db).claim(row["first_turn_id"], "new-runtime")
    store.finish(row["first_turn_id"], "runtime-example", "completed")
    assert not store.claim(row["first_turn_id"], "new-runtime")
    assert accept(db)["state"] == "completed"


def test_status_follows_compression_and_archive_without_resending(db):
    row = accept(db)
    sid = row["stored_session_id"]
    db.end_session(sid, "compression")
    db.create_session("child-example", source="desktop", parent_session_id=sid)
    db.set_session_archived("child-example", True)
    status = StartStore(db).status(owner(), "example", "request-example")
    assert status["resolved_session_id"] == "child-example"
    assert status["archived"] is True
    assert status["first_turn_state"] == "queued"
    assert status["active_turn"] is False
    assert accept(db)["stored_session_id"] == sid


def test_status_missing_runtime_is_unknown_not_completed(db):
    row = accept(db)
    store = StartStore(db)
    store.claim(row["first_turn_id"], "dead-runtime")
    status = store.status(owner(), "example", "request-example")
    assert status["first_turn_state"] == "running"
    assert status["reconciliation"] == "unknown_outcome"
    assert status["active_turn"] is False


def test_status_never_exposes_another_owner(db):
    accept(db)
    assert StartStore(db).status(owner(principal="someone-else"), "example", "request-example") is None


@pytest.mark.parametrize("change", ["archive", "delete", "new_turn", "compression"])
def test_queued_start_never_dispatches_into_changed_session(db, change):
    row = accept(db)
    sid = row["stored_session_id"]
    if change == "archive":
        db.set_session_archived(sid, True)
    elif change == "delete":
        db.delete_session(sid)
    elif change == "new_turn":
        db.append_message(sid, "assistant", "A different turn has run.")
    else:
        db.end_session(sid, "compression")
        db.create_session("child-example", source="desktop", parent_session_id=sid)
    svc = service(db)
    register_example(svc)
    result = svc.start(owner(), "example", "request-example")
    assert svc.runtime.scheduled == []
    assert result["first_turn_state"] == "cancelled"


def test_real_turn_adopts_first_user_row_and_commits_receipt(db, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from tui_gateway import server
    from tui_gateway.session_start_requests import GatewayStartRuntime
    from agent.context_compressor import _DB_PERSISTED_MARKER
    runtime = GatewayStartRuntime(server, None)
    svc = service(db, runtime)
    register_example(svc)
    calls = []
    records = []
    monkeypatch.setattr(runtime, "check_supported", lambda who: None)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_default_session_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(server, "_session_cwd", lambda record: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda record: None)
    monkeypatch.setattr(server, "_start_session_work", lambda fn, **kw: fn())
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda *args: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    monkeypatch.setattr(server, "_emit", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_sessions", {})
    def build(sid):
        record = server._sessions[sid]
        records.append(record)
        agent = SimpleNamespace(session_id=record["session_key"], clear_interrupt=lambda: None)
        def converse(*args, **kwargs):
            staged = agent._pending_cli_user_message
            assert staged[_DB_PERSISTED_MARKER]
            assert staged["message_uid"]
            calls.append(staged["_row_id"])
            assert record["history"] == []
            # Fake model only: exercise the real gateway admission, adoption,
            # terminal receipt and cleanup without an LLM or live credentials.
            return {"final_response": "Inspected.", "messages": [staged], "completed": True}
        agent.run_conversation = converse
        record["agent"] = agent
        record["agent_ready"].set()
    monkeypatch.setattr(server, "_schedule_agent_build", build)
    result = svc.start(owner(), "example", "request-example")
    assert len(calls) == 1
    assert result["first_turn_state"] == "completed"
    assert len(db.get_messages(result["stored_session_id"])) == 1
    assert not records[0]["running"]
    assert svc.start(owner(), "example", "request-example") == result
    assert len(calls) == 1


@pytest.mark.parametrize("spawn_refused", [False, True])
def test_gateway_adapter_stages_existing_row_and_uses_turn_lifecycle(db, monkeypatch, spawn_refused):
    from tui_gateway import server
    from tui_gateway.session_start_requests import GatewayStartRuntime
    from agent.context_compressor import _DB_PERSISTED_MARKER
    runtime = GatewayStartRuntime(server, None)
    svc = service(db, runtime)
    register_example(svc)
    observed = []
    monkeypatch.setattr(runtime, "check_supported", lambda who: None)
    monkeypatch.setattr(server, "_default_session_cwd", lambda: "/example")
    monkeypatch.setattr(server, "_register_session_cwd", lambda record: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda sid: None)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *args: None)
    monkeypatch.setattr(server, "_start_session_work", lambda fn, **kw: None if spawn_refused else (fn(), object())[1])
    records = []
    def publish(sid, key, record, lease):
        assert record["running"] is True, "first turn must be reserved BEFORE publication"
        records.append(record)
    def admit(rid, sid, record, *args):
        record["running"] = True
        return None, {}
    monkeypatch.setattr(server, "_lock_in_submit_turn", admit)
    monkeypatch.setattr(server, "_claim_or_reuse_live", publish)
    def run(rid, sid, record, text, **kwargs):
        staged = record["_submit_user_row"]
        assert staged[_DB_PERSISTED_MARKER]
        assert staged["content"] == text
        assert record["history"] == []
        assert record["auth_user_id"] == owner().principal
        assert len(db.get_messages(record["session_key"])) == 1
        observed.append(staged)
        kwargs["terminal_callback"]({"status": "settled"})
        return True
    monkeypatch.setattr(server, "_run_prompt_submit", run)
    status = svc.start(owner(), "example", "request-example")
    if spawn_refused:
        assert status["first_turn_state"] == "failed"
        assert observed == []
        assert not records[0]["running"]
        return
    assert status["first_turn_state"] == "completed"
    assert len(observed) == 1
    assert observed[0]["message_uid"] == status["first_turn_id"]
    assert svc.start(owner(), "example", "request-example") == status
    assert len(observed) == 1


def test_rpc_uses_server_owner_and_rejects_client_prompt(db, monkeypatch):
    from types import SimpleNamespace
    from tui_gateway import server
    from tui_gateway.transport import bind_transport, reset_transport
    svc = service(db)
    register_example(svc)
    monkeypatch.setattr(server, "get_session_start_service", lambda profile: svc)
    monkeypatch.setattr(server, "_start_request_owner", lambda profile: owner())
    token = bind_transport(SimpleNamespace(auth_identity={"user_id": "example", "provider": "basic"}))
    try:
        params = dict(profile="default", namespace="example", start_request_id="request-example")
        for extra in ({"text": "injected"}, {"principal": "other"}, {"session_id": "unrelated"}):
            assert server._methods["session.start_request"](1, {**params, **extra})["error"]["code"] == -32602
        assert svc.runtime.scheduled == []
        response = server._methods["session.start_request"](2, params)
        assert response["result"]["gateway_id"] == owner().gateway_id
        assert len(svc.runtime.scheduled) == 1
        assert server._methods["session.start_request"](3, params)["result"] == response["result"]
    finally:
        reset_transport(token)


def test_rpc_missing_authenticated_principal_is_refused(monkeypatch):
    from tui_gateway import server
    from tui_gateway.transport import bind_transport, reset_transport
    token = bind_transport(None)
    try:
        response = server._methods["session.start_request"](1, dict(
            profile="default", namespace="example", start_request_id="request-example"))
        assert response["error"]["code"] == 4030
    finally:
        reset_transport(token)


class FakeStartRuntime:
    def __init__(self):
        self.scheduled = []
        self.supported = True

    def check_supported(self, owner):
        if not self.supported:
            raise NotImplementedError("unsupported runtime")

    def schedule(self, row, intent, finish):
        self.scheduled.append((row, intent, finish))


def service(db, runtime=None):
    from tui_gateway.session_start_requests import StartService
    return StartService(StartStore(db), runtime or FakeStartRuntime())


def register_example(svc, resolve=None):
    return svc.register(owner(), "example", resolve or (
        lambda who, request: StartIntent(title="Investigate example", text="Inspect only.")))


def test_service_reauthorizes_retries_and_status(db):
    svc = service(db)
    authorized = True
    def resolve(who, request):
        assert who == owner() and request == "request-example"
        if not authorized:
            raise PermissionError("revoked")
        return StartIntent(title="Investigate example", text="Inspect only.")
    register_example(svc, resolve)
    first = svc.start(owner(), "example", "request-example")
    assert first["first_turn_state"] == "running"
    assert len(svc.runtime.scheduled) == 1
    assert svc.start(owner(), "example", "request-example") == first
    authorized = False
    for action in (svc.start, svc.status):
        with pytest.raises(PermissionError, match="revoked"):
            action(owner(), "example", "request-example")
    assert len(svc.runtime.scheduled) == 1


def test_service_registration_is_scoped_and_disposable(db):
    svc = service(db)
    unregister = register_example(svc)
    with pytest.raises(ValueError, match="registered"):
        register_example(svc)
    for who in (owner(profile="other"), owner(principal="other"), owner(gateway_id="other")):
        with pytest.raises(PermissionError):
            svc.start(who, "example", "request-example")
    unregister()
    unregister()
    with pytest.raises(PermissionError):
        svc.start(owner(), "example", "request-example")
    assert db._read_one("SELECT COUNT(*) FROM sessions")[0] == 0


def test_existing_start_reconciles_when_runtime_support_changes(db):
    svc = service(db)
    register_example(svc)
    status = svc.start(owner(), "example", "request-example")
    svc.runtime.supported = False
    assert svc.start(owner(), "example", "request-example") == status
    assert len(svc.runtime.scheduled) == 1


def test_importable_plugin_service_entry(monkeypatch):
    from tui_gateway import server
    from tui_gateway.session_start_requests import get_service
    sentinel = object()
    profiles = []
    monkeypatch.setattr(server, "get_session_start_service", lambda profile: (profiles.append(profile), sentinel)[1])
    assert get_service("example") is sentinel
    assert profiles == ["example"]


def test_service_unsupported_runtime_has_no_side_effect(db):
    svc = service(db)
    register_example(svc)
    svc.runtime.supported = False
    with pytest.raises(NotImplementedError, match="unsupported"):
        svc.start(owner(), "example", "request-example")
    assert db._read_one("SELECT COUNT(*) FROM sessions")[0] == 0


def test_service_concurrent_starts_schedule_only_once(db):
    svc = service(db)
    register_example(svc)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: svc.start(owner(), "example", "request-example"), range(8)))
    assert len({r["stored_session_id"] for r in results}) == 1
    assert len(svc.runtime.scheduled) == 1
    row, intent, finish = svc.runtime.scheduled[0]
    assert db.get_messages(row["stored_session_id"])[0]["content"] == intent.text
    finish("completed")
    assert svc.status(owner(), "example", "request-example")["first_turn_state"] == "completed"


def test_service_restart_recovers_queued_but_never_claimed(db):
    queued = accept(db)
    svc = service(db)
    register_example(svc)
    first = svc.start(owner(), "example", "request-example")
    assert first["first_turn_id"] == queued["first_turn_id"]
    assert len(svc.runtime.scheduled) == 1
    restarted = service(db)
    register_example(restarted)
    assert restarted.start(owner(), "example", "request-example") == first
    assert restarted.runtime.scheduled == []


def test_service_ambiguous_schedule_failure_is_not_replayed(db):
    svc = service(db)
    register_example(svc)
    def crash(row, intent, finish):
        raise RuntimeError("simulated process failure after claim")
    svc.runtime.schedule = crash
    with pytest.raises(RuntimeError, match="simulated"):
        svc.start(owner(), "example", "request-example")
    restarted = service(db)
    register_example(restarted)
    status = restarted.start(owner(), "example", "request-example")
    assert status["reconciliation"] == "unknown_outcome"
    assert restarted.runtime.scheduled == []


def test_same_session_id_on_two_gateways_is_owner_local(db, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from itertools import cycle
    import tui_gateway.session_start_requests as starts
    other_db = SessionDB(tmp_path / "other.db")
    values = cycle(["same-session-example", "same-turn-example"])
    monkeypatch.setattr(starts.uuid, "uuid4", lambda: SimpleNamespace(hex=next(values)))
    try:
        first = accept(db)
        second = accept(other_db, owner=owner(gateway_id="gateway-other"))
        assert first["stored_session_id"] == second["stored_session_id"]
        other_db.set_session_archived(second["stored_session_id"], True)
        assert not StartStore(db).status(owner(), "example", "request-example")["archived"]
        assert StartStore(other_db).status(owner(gateway_id="gateway-other"), "example", "request-example")["archived"]
        assert StartStore(other_db).status(owner(), "example", "request-example") is None
    finally:
        other_db.close()


def test_status_observes_durable_active_turn_lease(db):
    row = accept(db)
    store = StartStore(db)
    assert store.claim(row["first_turn_id"], "runtime-example")
    assert db.try_acquire_session_turn_lease(row["stored_session_id"], "external-example")
    status = store.status(owner(), "example", "request-example")
    assert status["active_turn"]
    assert status["reconciliation"] == "running"


@pytest.mark.parametrize("isolated", [True, False])
def test_gateway_runtime_refuses_isolation_and_profile_mismatch(monkeypatch, isolated):
    import contextlib
    from tui_gateway import server
    from tui_gateway.session_start_requests import GatewayStartRuntime
    monkeypatch.setattr(server, "_profile_build_scope", lambda home: contextlib.nullcontext())
    monkeypatch.setattr(server, "_turn_isolation_enabled", lambda: isolated)
    monkeypatch.setattr(server, "_response_profile_name", lambda profile: "other")
    with pytest.raises(NotImplementedError if isolated else PermissionError):
        GatewayStartRuntime(server, None).check_supported(owner())


def test_wrong_runtime_cannot_finish_turn(db):
    row = accept(db)
    store = StartStore(db)
    assert store.claim(row["first_turn_id"], "runtime-example")
    assert not store.finish(row["first_turn_id"], "wrong-runtime", "completed")
    assert accept(db)["state"] == "running"
