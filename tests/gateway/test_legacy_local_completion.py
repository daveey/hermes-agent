"""A legacy async delegation names its Desktop parent by the raw session id.

Exactly one serving authority's verified local receipt (and ledger row) turns that raw key into the canonical
local route, BEFORE the routing scope is chosen; API-server events, other profiles, retiring or unserved
authorities, a row in another ledger and ambiguous owners stay as they were.

Fixtures are realistic: ``authority.profile_id`` is the profile HOME path (``gateway/run_runtime.py``), the
launch and a served secondary each own a ``state.db``, ledger and route namespace.
"""
from types import SimpleNamespace

import pytest

from tools import async_delegation as delegation


def _actor(home):
    from gateway.session_contract import SessionActor
    return SessionActor("test-owner", str(home), frozenset({"session:create", "session:submit", "session:read"}),
                        "fixture")


async def _rig(tmp_path, monkeypatch, *, secondary=True):
    """A multiplexing runner: launch home plus (optionally) one served secondary, each with a local session."""
    import gateway.run as gw
    import hermes_state
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner, _profile_runtime_scope
    from gateway.session_authorities import SessionAuthorities
    from gateway.session_authority import SessionAuthority, initialize_session_authority
    from gateway.session_local import create_local_session
    from hermes_state import SessionDB

    root = tmp_path / "root"
    sec_home = root / "profiles" / "secondary"
    sec_home.mkdir(parents=True)
    (sec_home / "config.yaml").write_text("{}\n")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_BASE_HOME", str(root))
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    monkeypatch.setattr(gw, "_hermes_home", root)
    monkeypatch.setattr(SessionAuthority, "_schedule", lambda self, ref: None)  # admitted rows stay queued
    runner = GatewayRunner(GatewayConfig(multiplex_profiles=True, sessions_dir=tmp_path / "sessions"))
    db = SessionDB(root / "state.db")  # the runner's own handles resolve per scope, as in production
    registry = SessionAuthorities(root, multiplexed=True)
    runner.session_authorities = registry
    launch = await initialize_session_authority(runner, profile_id=str(root), instance_id="launch", db=db)
    registry.add(root, launch)
    assert launch.profile_id == str(root)  # production: a HOME path, never the profile name
    rig = SimpleNamespace(runner=runner, registry=registry, launch=launch, root=root, sec_home=sec_home,
                          served=None, scope=_profile_runtime_scope)
    rig.launch_ref = create_local_session(launch, _actor(root), dict(
        request_id="launch", source="cli", model="fixture", toolsets=[]))
    if secondary:
        registry.add(sec_home, None, name="secondary")
        sdb = SessionDB(sec_home / "state.db")
        rig.served = await initialize_session_authority(
            runner, profile_id=str(sec_home), instance_id="served", register=False, db=sdb)
        registry.replace(sec_home, rig.served)
        rig.served_ref = create_local_session(rig.served, _actor(sec_home), dict(
            request_id="served", source="cli", model="fixture", toolsets=[]))
    monkeypatch.setattr(runner, "_handle_message", _refuse_turn)
    # Upstream runs an unviewed final result as the parent's turn (``resumes_parent``,
    # test_unviewed_completion_resumes_parent.py). These cases cover legacy ROUTE resolution, so keep the
    # outcome a note: its row lands in the resolved session, where the assertions can see which one.
    import tools.process_registry_notifications as notifications
    monkeypatch.setattr(notifications, "completion_resumes_parent", lambda evt: False)
    return rig


async def _refuse_turn(event):
    raise AssertionError("no viewer: the completion must be a note, not a turn")


def _legacy(home, sid, name, scope=None, **extra):
    """A legacy row exactly as the producer persisted it: raw Desktop id, no route fields, in *home*'s ledger."""
    from gateway.run import _profile_runtime_scope
    from tests.gateway.test_completion_admission import pending
    with _profile_runtime_scope(home):
        return dict(pending(sid, name), parent_session_id=sid, **extra)


def _row(home, evt):
    from gateway.run import _profile_runtime_scope
    with _profile_runtime_scope(home):
        return delegation.get_durable_delegation(evt["delegation_id"])


def _notes(authority, evt, target=None):
    from gateway.session_local_recovery import owned_receipt
    target = target or owned_receipt(authority, evt["parent_session_id"])[0]["entry"]["session_id"]
    return [m for m in authority.db.get_messages(target) if evt["delegation_id"] in str(m.get("content"))]


async def _drain(authority, ref):
    """The admitted turn's drain, run in its owner's scope as production's ``_schedule`` task does."""
    from gateway.session_authorities import owner_scope
    with owner_scope(authority):
        await authority._drain(ref)


def _admitted(authority, sid):
    from hermes_state_runtime import list_session_admissions
    return [r for r in list_session_admissions(authority.db, session_id=sid, pending_only=False)
            if r["principal_id"].startswith("automation:")]


async def _delivered_once(rig, authority, home, ref, evt):
    """Group delivery from the UNSCOPED root (the supervised watcher's scope), then a replay of the same row."""
    assert await rig.runner._deliver_async_delegation_group([evt]) is True
    await _drain(authority, ref)
    assert [_row(home, evt)[k] for k in ("delivery_state", "delivery_attempts")] == ["delivered", 1]
    assert len(_notes(authority, evt)) == 1 and len(_admitted(authority, ref.session_id)) == 1
    await rig.runner._deliver_async_delegation_group([evt])
    await _drain(authority, ref)
    assert len(_notes(authority, evt)) == 1 and len(_admitted(authority, ref.session_id)) == 1
    assert _row(home, evt)["delivery_attempts"] == 1


def _close(rig):
    for authority in (rig.launch, rig.served):
        if authority is not None:
            authority.db.close()


@pytest.mark.asyncio
async def test_launch_raw_desktop_key_resolves_with_home_path_profile_id(tmp_path, monkeypatch):
    rig = await _rig(tmp_path, monkeypatch, secondary=False)
    try:
        evt = _legacy(rig.root, rig.launch_ref.session_id, "deleg_legacy_launch")
        await _delivered_once(rig, rig.launch, rig.root, rig.launch_ref, evt)
    finally:
        _close(rig)


@pytest.mark.asyncio
async def test_single_completion_notification_path_resolves_too(tmp_path, monkeypatch):
    rig = await _rig(tmp_path, monkeypatch, secondary=False)
    try:
        evt = _legacy(rig.root, rig.launch_ref.session_id, "deleg_legacy_single")
        assert await rig.runner._deliver_completion_notification("[finished] deleg_legacy_single", evt) is True
        await _drain(rig.launch, rig.launch_ref)
        assert len(_notes(rig.launch, evt)) == 1
    finally:
        _close(rig)


@pytest.mark.asyncio
async def test_served_secondary_event_is_scoped_and_claimed_in_its_own_ledger(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    rig = await _rig(tmp_path, monkeypatch)
    try:
        evt = _legacy(rig.sec_home, rig.served_ref.session_id, "deleg_legacy_secondary")
        async with rig.runner._completion_event_scope(evt):  # selected from the resolved owner, not the raw key
            assert get_hermes_home() == rig.sec_home
        await _delivered_once(rig, rig.served, rig.sec_home, rig.served_ref, evt)
        assert _row(rig.root, evt) is None  # the launch ledger never saw it, so nothing could claim there
    finally:
        _close(rig)


@pytest.mark.asyncio
async def test_served_secondary_single_path_and_retired_session(tmp_path, monkeypatch):
    rig = await _rig(tmp_path, monkeypatch)
    try:
        sid = rig.served_ref.session_id
        live = rig.served.sessions[sid]
        target = rig.runner.session_store.lookup_by_session_key(live.route).session_id
        rig.served.db.end_session(target, "user_exit")
        rig.served.sessions.pop(sid)  # boot left the ended session unrestored
        evt = _legacy(rig.sec_home, sid, "deleg_legacy_secondary_retired")
        assert await rig.runner._deliver_completion_notification("[finished] deleg_legacy_secondary_retired", evt) is True
        await _drain(rig.served, rig.served_ref)
        assert sid in rig.served.sessions
        assert _row(rig.sec_home, evt)["delivery_state"] == "delivered"
        assert len(_notes(rig.served, evt, target)) == 1 and len(_admitted(rig.served, sid)) == 1
    finally:
        _close(rig)


@pytest.mark.asyncio
async def test_served_secondary_compressed_session_delivers_to_the_tip(tmp_path, monkeypatch):
    from hermes_state_local_lineage import advance_local_target
    from gateway.session_local_recovery import owned_receipt
    rig = await _rig(tmp_path, monkeypatch)
    try:
        sid = rig.served_ref.session_id
        receipt = owned_receipt(rig.served, sid)[0]
        tip, tip_child = receipt["entry"]["session_id"], "compressed-tip"
        db = rig.served.db
        db.end_session(tip, "compression")
        db.create_session(tip_child, "local", session_key=receipt["route"], chat_id=sid,
                          user_id=receipt["principal_id"], parent_session_id=tip)
        db._execute_write(lambda conn: advance_local_target(conn, tip, tip_child))
        assert owned_receipt(rig.served, sid)[0]["entry"]["session_id"] == tip_child
        evt = _legacy(rig.sec_home, sid, "deleg_legacy_secondary_compressed")
        assert await rig.runner._deliver_async_delegation_group([evt]) is True
        await _drain(rig.served, rig.served_ref)
        assert _row(rig.sec_home, evt)["delivery_state"] == "delivered"
        assert len(_notes(rig.served, evt)) == 1
        assert not [m for m in db.get_messages(tip) if evt["delegation_id"] in str(m.get("content"))]
    finally:
        _close(rig)


@pytest.mark.asyncio
async def test_ambiguous_owners_fail_closed(tmp_path, monkeypatch):
    from gateway.completion_local_route import resolve_legacy_local_event
    rig = await _rig(tmp_path, monkeypatch)
    try:
        dup = rig.root / "profiles" / "dup"
        dup.mkdir()
        # A second home serving the SAME store under the same namespace: both verify the one receipt.
        rig.registry.add(dup, SimpleNamespace(profile_id=str(dup), db=rig.served.db, runner=rig.runner), name="secondary")
        evt = _legacy(rig.sec_home, rig.served_ref.session_id, "deleg_legacy_ambiguous")
        assert resolve_legacy_local_event(rig.runner, evt) is evt
        rig.registry.remove(dup)
        assert resolve_legacy_local_event(rig.runner, evt)["session_key"].startswith("agent:secondary:local:")
    finally:
        _close(rig)


@pytest.mark.asyncio
async def test_row_in_another_ledger_is_never_claimed_through_the_receipt_owner(tmp_path, monkeypatch):
    """claim_completion_delivery answers True for a row missing from the ambient ledger: the resolver must not
    send a launch-ledger row into the secondary's scope, where that missing-row True would claim it falsely."""
    from gateway.completion_local_route import resolve_legacy_local_event
    rig = await _rig(tmp_path, monkeypatch)
    try:
        evt = _legacy(rig.root, rig.served_ref.session_id, "deleg_legacy_elsewhere")
        assert resolve_legacy_local_event(rig.runner, evt) is evt
        await rig.runner._deliver_async_delegation_group([evt])
        assert _row(rig.sec_home, evt) is None and not _admitted(rig.served, rig.served_ref.session_id)
        assert _row(rig.root, evt)["delivery_state"] == "pending"  # still the launch ledger's, unclaimed
        assert _row(rig.root, evt)["delivery_attempts"] == 0
    finally:
        _close(rig)


@pytest.mark.asyncio
async def test_retiring_or_unserved_owner_is_not_resolved(tmp_path, monkeypatch):
    from gateway.completion_local_route import resolve_legacy_local_event
    rig = await _rig(tmp_path, monkeypatch)
    try:
        evt = _legacy(rig.sec_home, rig.served_ref.session_id, "deleg_legacy_parked")
        assert resolve_legacy_local_event(rig.runner, evt)["session_key"].startswith("agent:secondary:local:")
        rig.served.retiring = True
        assert resolve_legacy_local_event(rig.runner, evt) is evt
        rig.served.retiring = False
        rig.registry.remove(rig.sec_home)  # no longer served at all
        assert resolve_legacy_local_event(rig.runner, evt) is evt
    finally:
        _close(rig)


@pytest.mark.asyncio
async def test_resolver_negatives_leave_the_event_alone(tmp_path, monkeypatch):
    from gateway.completion_local_route import resolve_legacy_local_event
    rig = await _rig(tmp_path, monkeypatch)
    try:
        sid = rig.served_ref.session_id
        base = _legacy(rig.sec_home, sid, "deleg_legacy_negatives")
        resolved = resolve_legacy_local_event(rig.runner, base)
        assert resolved["session_key"].startswith("agent:secondary:local:") and resolved["session_key"] != sid
        assert base["session_key"] == sid  # never mutates the producer's row
        negatives = {
            "other event": {**base, "type": "process"},
            "no delegation id": {k: v for k, v in base.items() if k != "delegation_id"},
            "no ledger row": {**base, "delegation_id": "deleg_never_persisted"},
            "thread id": {**base, "thread_id": "thread"},
            "whitespace id": {**base, "session_key": " " + sid, "parent_session_id": " " + sid},
            "no receipt": {**base, "session_key": "opaque-client", "parent_session_id": "opaque-client"},
            "launch's own receipt id": {**base, "session_key": rig.launch_ref.session_id,
                                        "parent_session_id": rig.launch_ref.session_id},  # row is not in launch
            "parent differs": {**base, "parent_session_id": "other"},
            "no parent": {k: v for k, v in base.items() if k != "parent_session_id"},
            "explicit api platform": {**base, "platform": "api_server", "chat_id": sid},
            "api platform only": {**base, "platform": "api_server"},
            "chat id": {**base, "chat_id": "1"},
            "chat type": {**base, "chat_type": "dm"},
            "origin session": {**base, "origin_session_id": sid},
            "structured key": {**base, "session_key": resolved["session_key"]},
        }
        for label, evt in negatives.items():
            assert resolve_legacy_local_event(rig.runner, evt) is evt, label
    finally:
        _close(rig)


@pytest.mark.asyncio
async def test_explicit_api_event_keeps_its_route_through_delivery(tmp_path, monkeypatch):
    rig = await _rig(tmp_path, monkeypatch)
    try:
        sid = rig.served_ref.session_id
        evt = _legacy(rig.sec_home, sid, "deleg_legacy_api", platform="api_server", chat_id=sid)
        await rig.runner._deliver_async_delegation_group([evt])
        assert not _admitted(rig.served, sid) and not _admitted(rig.launch, sid)
        unknown = _legacy(rig.root, "opaque-client-session", "deleg_legacy_unknown")
        assert await rig.runner._deliver_async_delegation_group([unknown]) is None  # unchanged: terminal drop
    finally:
        _close(rig)
