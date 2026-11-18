"""Strict local creation must commit custody and the task together."""
import json
from types import SimpleNamespace

import pytest

from agent.origin_turn_custody import scope
from agent.turn_facade_lease import admit_durable_turn_lease
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_origin, kanban_origin_native as native
from hermes_cli import kanban_origin_admission as admission
from hermes_state import SessionDB
from tests.tools import test_kanban_worker_created_chats as fixtures
from tools import kanban_tools


@pytest.fixture
def turn(tmp_path, monkeypatch):
    home = fixtures.make_home(tmp_path, monkeypatch)
    from hermes_cli import kanban_db_sync as sync
    with kbc.connect_closing() as conn:
        sync.set_mode(conn, "primary")
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("here", {"here"}))
    monkeypatch.setenv("HERMES_SESSION_ID", fixtures.CHAT)
    monkeypatch.setattr("agent.turn_liveness.resolve_turn_liveness_settings", lambda cfg: (None, 1.0))
    db = SessionDB(home / "state.db")
    agent = SimpleNamespace(_session_db=db, session_id=fixtures.CHAT, _persist_disabled=False,
                            _interrupt_requested=False, _emit_status=lambda *a: None,
                            _touch_activity=lambda *a: None)
    lease = admit_durable_turn_lease(agent, session_id=fixtures.CHAT, relay_turn_id="strict-create",
        task_context={"platform": "desktop", "session_id": fixtures.CHAT}, conversation_history=[]).lease
    assert lease is not None
    try:
        lease.start()
        with scope(lease):
            yield lease
    finally:
        lease.stop_refresher()
        lease.join_threads()
        lease.release()
        db.close()


def create(**extra):
    return json.loads(kanban_tools._handle_create({"title": "strict child", "assignee": "default",
                                                 "requires": [], "placement": "local", **extra}))


def snapshot(conn):
    return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in ("tasks", "task_events", "task_links", "kanban_notify_subs", "task_chat_links")}


@pytest.mark.parametrize("revocation", ["before_capture", "at_record", "none"])
def test_real_tool_create_is_atomic(turn, monkeypatch, revocation):
    record = native.record
    def recording(conn, task_id, captured, created_at):
        if revocation == "at_record":
            turn.release()
        return record(conn, task_id, captured, created_at)
    monkeypatch.setattr(native, "record", recording)
    with kbc.connect_closing() as conn:
        before = snapshot(conn)
    with admission.strict_local_creation():
        if revocation == "before_capture":
            turn.release()
        result = create()
    with kbc.connect_closing() as conn:
        if revocation == "none":
            assert result.get("ok"), result
            assert native._receipt(conn, result["task_id"])["origin"]["session"] == fixtures.CHAT
        else:
            assert not result.get("ok"), result
            assert snapshot(conn) == before


def test_no_ambient_authority(turn):
    turn.release()
    with pytest.raises(admission.AdmissionRefused):
        with admission.strict_local_creation():
            pytest.fail("admitted dead custody")


@pytest.mark.parametrize("mode", ["missing", "foreign", "same"])
def test_idempotency_requires_matching_receipt(turn, mode):
    captured = native.capture(None)
    with kbc.connect_closing() as conn:
        task = kb.create_task(conn, title="existing", idempotency_key="strict-key",
                              native_origin=captured if mode != "missing" else None)
        if mode == "foreign":
            binding = native._receipt(conn, task)
            binding["origin"]["session"] = "another-chat"
            conn.execute("UPDATE origin_native_receipts SET binding_json=? WHERE task_id=?",
                         (json.dumps(binding), task))
            conn.commit()
        before = snapshot(conn)
        with admission.strict_local_creation():
            if mode == "same":
                assert kb.create_task(conn, title="retry", idempotency_key="strict-key",
                                      native_origin=captured) == task
            else:
                with pytest.raises(admission.AdmissionRefused):
                    kb.create_task(conn, title="retry", idempotency_key="strict-key", native_origin=captured)
        assert snapshot(conn) == before


def test_nested_transaction_is_not_supported(turn):
    with kbc.connect_closing() as conn, kb.write_txn(conn):
        with admission.strict_local_creation(), pytest.raises(admission.AdmissionRefused):
            kb.create_task(conn, title="nested", native_origin=native.capture(None))


@pytest.mark.parametrize("field,value", [("placement", "auto"), ("placement", None),
                                          ("requires", ["gateway:x"]), ("runs_on", ["machine:x"]),
                                          ("placement_policy", "worker-first-v1"),
                                          ("origin_session", "@session:default@here/x"),
                                          ("self_work", {"anything": True})])
def test_strict_tool_refuses_nonlocal_side_effect_paths(turn, monkeypatch, field, value):
    def forbidden(*args, **kwargs):
        pytest.fail("reached placement or origin resolution before strict refusal")
    monkeypatch.setattr(kanban_tools, "_place_remote", forbidden)
    monkeypatch.setattr(kanban_origin, "resolve", forbidden)
    monkeypatch.setattr("hermes_cli.kanban_selfwork_outbound.submit", forbidden)
    with admission.strict_local_creation():
        result = create(**{field: value})
    assert not result.get("ok"), result


def test_missing_primary_scope_rolls_back(turn, monkeypatch):
    monkeypatch.setattr(native, "_primary_scope", lambda conn: None)
    with kbc.connect_closing() as conn:
        before = snapshot(conn)
        with admission.strict_local_creation(), pytest.raises(admission.AdmissionRefused):
            kb.create_task(conn, title="no primary", native_origin=native.capture(None))
        assert snapshot(conn) == before


def test_legacy_optional_receipt_still_supported(turn):
    turn.release()
    result = create()
    assert result.get("ok"), result
    with kbc.connect_closing() as conn:
        assert native._receipt(conn, result["task_id"]) is None


@pytest.mark.parametrize("intent", ["origin_intent", "selfwork_intent"])
def test_courier_inputs_refused_before_preflight(turn, monkeypatch, intent):
    def forbidden(*args, **kwargs):
        pytest.fail("courier request reached origin preflight")
    monkeypatch.setattr(kanban_origin, "resolve", forbidden)
    with admission.strict_local_creation():
        result = json.loads(kanban_tools._handle_create(
            {"title": "courier", "placement": "local"}, **{intent: {"request_id": "x"}}))
    assert not result.get("ok"), result


def test_overlap_success_is_not_native_admission(turn, monkeypatch):
    with kbc.connect_closing() as conn:
        task = kb.create_task(conn, title="legacy overlap")
    monkeypatch.setattr(kanban_tools.kanban_overlap, "check", lambda *a, **kw:
                        (json.dumps({"ok": True, "task_id": task}), None))
    with admission.strict_local_creation():
        result = create()
    assert not result.get("ok"), result


def test_previous_admission_cannot_authorize_later_dedup(turn, monkeypatch):
    with admission.strict_local_creation():
        first = create()
        assert first.get("ok"), first
        monkeypatch.setattr(kanban_tools.kanban_overlap, "check", lambda *a, **kw:
                            (json.dumps(first), None))
        result = create()
    assert not result.get("ok"), result


def test_valid_receipt_with_wrong_owner_is_refused(turn):
    with kbc.connect_closing() as conn:
        captured = native.capture(None)
        task = kb.create_task(conn, title="owner", idempotency_key="owner-key", native_origin=captured)
        binding = native._receipt(conn, task)
        binding["owner"]["profile"] = "other"
        binding["task_record_key"] = f"other/default/{task}"
        conn.execute("UPDATE origin_native_receipts SET binding_json=? WHERE task_id=?",
                     (json.dumps(binding), task))
        conn.commit()
        with admission.strict_local_creation(), pytest.raises(admission.AdmissionRefused):
            kb.create_task(conn, title="replay", idempotency_key="owner-key", native_origin=captured)


def test_ambient_origin_refused_before_resolution(turn, monkeypatch):
    monkeypatch.setenv("HERMES_ORIGIN_SESSION", "@session:default@here/ambient")
    def forbidden(*args, **kwargs):
        pytest.fail("ambient origin reached resolution")
    monkeypatch.setattr(kanban_origin, "resolve", forbidden)
    with admission.strict_local_creation():
        result = create()
    assert not result.get("ok"), result


@pytest.mark.parametrize("revocation", ["turn", "receipt"])
def test_idempotent_replay_revalidates_custody(turn, revocation):
    with kbc.connect_closing() as conn:
        captured = native.capture(None)
        task = kb.create_task(conn, title="first", idempotency_key="revoke-key", native_origin=captured)
        with admission.strict_local_creation():
            if revocation == "turn":
                turn.release()
            else:
                conn.execute("DELETE FROM origin_native_receipts WHERE task_id=?", (task,))
                conn.commit()
            before = snapshot(conn)
            with pytest.raises(admission.AdmissionRefused):
                kb.create_task(conn, title="replay", idempotency_key="revoke-key", native_origin=captured)
            assert snapshot(conn) == before


def test_copied_thread_context_preserves_live_turn(turn):
    from contextvars import copy_context
    from concurrent.futures import ThreadPoolExecutor
    context = copy_context()
    def in_thread():
        with admission.strict_local_creation():
            return create()
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(context.run, in_thread).result(timeout=20)
    assert result.get("ok"), result
    with kbc.connect_closing() as conn:
        assert native._receipt(conn, result["task_id"])["origin"]["session"] == fixtures.CHAT


def test_uncopied_thread_has_no_authority(turn):
    from concurrent.futures import ThreadPoolExecutor
    def in_thread():
        with pytest.raises(admission.AdmissionRefused), admission.strict_local_creation():
            pytest.fail("thread acquired ambient authority")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(in_thread).result(timeout=20)


def test_db_origin_intent_refused(turn):
    with kbc.connect_closing() as conn:
        with admission.strict_local_creation(), pytest.raises(admission.AdmissionRefused):
            kb.create_task(conn, title="courier", origin_intent={"request_id": "x"})
