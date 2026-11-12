"""``session.resume`` materializes a row for a minted-but-never-persisted key.

Bug class: the backend dies between ``session.create`` (which intentionally
writes no state.db row until the first prompt — no "Untitled" litter) and that
first ``prompt.submit``. The stored key exists client-side (pinned tile,
sidebar) but has no row anywhere, and after a restart the in-memory live-lazy
lookup can't find it either — so resume used to 4007 forever and the desktop
surfaced "no route to the backend holding this session".

The fix: when the resume target is a well-formed server-minted key
(``%Y%m%d_%H%M%S_`` + 6 hex, the exact shape ``_new_session_key()`` produces),
materialize the row and continue the normal empty resume instead of failing.
Abandoned drafts still leave no row — nothing is written until a client
explicitly resumes the exact key. Fail-closed: garbage, 8-hex runtime ids,
and titles still 4007, and lazy subagent watch windows are excluded.

Multi-gateway rule: the key shape alone is not proof of ownership — every
gateway mints the same shape. Only a key THIS backend's store recorded at
``session.create`` (``minted_session_keys``) is materialized; a foreign
gateway's key must 4007 so the client routes the resume to its owner instead
of opening an empty, runtime-less phantom row here.
"""

from __future__ import annotations

import pytest

from hermes_state import SessionDB
from tui_gateway import server

MINTED_KEY = "20260828_053121_3427a9"  # the morning's lost session shape
EXISTING_KEY = "20260827_224618_956bf8"


@pytest.fixture()
def real_db(monkeypatch, tmp_path):
    """Real SessionDB behind ``_get_db``, with the heavy resume machinery off."""
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "test-model")
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda _key, _home=None: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda *a, **k: None)
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a, **k: None)
    monkeypatch.setattr(server, "_default_session_cwd", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(server, "_profile_configured_cwd", lambda _home: str(tmp_path))
    monkeypatch.setattr(server, "_child_run_active", lambda _key, _home=None: False)
    known = set(server._sessions)
    yield db
    with server._sessions_lock:
        for sid in [s for s in server._sessions if s not in known]:
            server._sessions.pop(sid, None)
    db.close()


def _resume(**params):
    return server.handle_request({"id": "1", "method": "session.resume", "params": params})


def _assert_resumed_ok(resp, target):
    assert "error" not in resp, resp
    assert resp["result"]["resumed"] == target
    assert resp["result"]["session_id"], "a live runtime must be registered"


def _create_then_restart() -> str:
    """``session.create`` on this backend, never prompted, then drop the live record
    (what a backend restart does to the in-memory registry). Returns the stored key."""
    created = server.handle_request({"id": "c", "method": "session.create", "params": {}})
    assert "error" not in created, created
    sid, key = created["result"]["session_id"], created["result"]["stored_session_id"]
    with server._sessions_lock:
        server._sessions.pop(sid, None)
    return key


def test_resume_materializes_row_for_minted_key(real_db):
    """The core fix: a key minted by THIS backend but never persisted resumes after a
    restart with empty history, and the row now exists."""
    key = _create_then_restart()
    assert server._is_server_minted_key(key)
    assert real_db.get_session(key) is None, "session.create must not write a sessions row"
    assert real_db.was_session_key_minted(key)

    resp = _resume(session_id=key)

    _assert_resumed_ok(resp, key)
    assert resp["result"]["message_count"] == 0
    row = real_db.get_session(key)
    assert row is not None, "resume must materialize the DB row"
    assert row["source"] == "tui"
    assert row["model"] == "test-model"


def test_resume_materializes_recorded_key(real_db):
    """A recorded minted key resumes; materialized row carries the launch profile."""
    real_db.record_minted_session_key(MINTED_KEY)
    resp = _resume(session_id=MINTED_KEY)

    _assert_resumed_ok(resp, MINTED_KEY)
    row = real_db.get_session(MINTED_KEY)
    assert row is not None, "resume must materialize the DB row"
    assert row["source"] == "tui"
    assert row["model"] == "test-model"
    # Materialization stamps the launch profile explicitly (the #99222 contract
    # _ensure_session_db_row applies on first-prompt persistence: profile_name_for_home
    # or _current_profile_name, never NULL). A bare create_session leaves NULL, so the
    # oracle is the production expression itself, not a bare insert.
    assert row.get("profile_name") == (
        server.profile_name_for_home(None) or server._current_profile_name())


def test_resume_existing_row_unaffected(real_db):
    """A persisted session resumes normally and its row is not duplicated."""
    real_db.create_session(EXISTING_KEY, source="tui", model="test-model")
    real_db.append_message(EXISTING_KEY, "user", "hello")

    resp = _resume(session_id=EXISTING_KEY)

    _assert_resumed_ok(resp, EXISTING_KEY)
    # message_count stays 1 — a second create would have been an INSERT-OR-IGNORE
    # no-op, but the row must remain the same single conversation.
    assert real_db.get_session(EXISTING_KEY)["message_count"] == 1


def test_resume_foreign_gateway_key_4007_without_phantom_row(real_db):
    """A well-formed server-style key this backend never minted (another gateway's
    session) must 4007 — the client then routes to the owning gateway — and must NOT
    leave a sessions row behind."""
    foreign = "20261013_184022_b1a59f"
    assert server._is_server_minted_key(foreign)

    resp = _resume(session_id=foreign)

    assert resp.get("error", {}).get("code") == 4007, resp
    assert real_db.get_session(foreign) is None
    assert len(real_db.list_sessions_rich(source="tui")) == 0


def test_resume_garbage_id_still_4007(real_db):
    """Non-key garbage stays fail-closed: same error, no write."""
    resp = _resume(session_id="not-a-session")

    assert resp["error"]["code"] == 4007
    # nothing materialized for arbitrary strings
    assert len(real_db.list_sessions_rich(source="tui")) == 0


def test_resume_8hex_runtime_id_still_4007(real_db):
    """8-hex runtime session ids (``uuid4().hex[:8]``) are NOT stored keys."""
    resp = _resume(session_id="5a81f231")

    assert resp["error"]["code"] == 4007
    assert len(real_db.list_sessions_rich(source="tui")) == 0


def test_lazy_resume_preserves_stored_model_without_building(real_db):
    real_db.create_session(EXISTING_KEY, source="desktop", model="claude-opus-5-5",
                           model_config={"provider": "anthropic", "model": "claude-opus-5-5"})
    resp = _resume(session_id=EXISTING_KEY, lazy=True)
    _assert_resumed_ok(resp, EXISTING_KEY)
    result = resp["result"]
    assert result["info"]["model"] == "claude-opus-5-5"
    assert result["info"]["provider"] == "anthropic"
    record = server._sessions[result["session_id"]]
    assert record["agent"] is None
    assert record["model_override"]["model"] == "claude-opus-5-5"


def test_resume_lazy_watch_missing_key_still_4007(real_db):
    """Lazy subagent watch windows must not mint phantom rows for dead children."""
    real_db.record_minted_session_key(MINTED_KEY)  # isolate the lazy gate
    resp = _resume(session_id=MINTED_KEY, lazy=True)

    assert resp["error"]["code"] == 4007
    assert real_db.get_session(MINTED_KEY) is None


def test_resume_minted_key_claimed_by_other_profile_fails_closed(real_db):
    """A key claimed by a live session under ANOTHER profile must not mint here.

    Mirrors test_resume_live_lazy_session's unscoped-resume fail-closed rule
    (#93296): the materialize gate must not turn a cross-profile leak into a
    phantom launch-store row. The key looks server-minted, but a live record
    owns it — routing guesses are forbidden.
    """
    record = {
        "history": [],
        "last_active": 0.0,
        "pending_title": "Bot Chat",
        "pending_hidden": True,
        "profile_home": "/tmp/other-profile",
        "running": False,
        "session_key": MINTED_KEY,
        "source": "desktop",
    }
    server._sessions["live-other-profile"] = record
    real_db.record_minted_session_key(MINTED_KEY)  # isolate the live-claim gate
    try:
        resp = _resume(session_id=MINTED_KEY)
        assert resp["error"]["code"] == 4007
        assert real_db.get_session(MINTED_KEY) is None
    finally:
        with server._sessions_lock:
            server._sessions.pop("live-other-profile", None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("20260828_053121_3427a9", True),
        ("20260827_224618_956bf8", True),
        ("5a81f231", False),  # 8-hex runtime sid
        ("fcd97d0c", False),  # 8-hex runtime sid
        ("not-a-session", False),
        ("", False),
        ("20260828_053121_3427", False),  # too-short hex tail
        ("20260828_053121_3427a9ZZ", False),  # trailing garbage
        ("Bot Chat", False),  # titles are not keys
    ],
)
def test_is_server_minted_key_shape(value, expected):
    assert server._is_server_minted_key(value) is expected


# ── Deleted chats are not unfinished drafts (desktop-03cd4d73b3) ──────────────────────────────────
# Startup recovery resumed a chat deleted after its writer exited: the missing row plus the surviving
# ``minted_session_keys`` record read as a never-persisted draft, so resume tried to materialize it, the
# resurrection fence (``insert_session_row_in_transaction``) refused with RuntimeStoreError('not_found'),
# and the refusal was logged as a WARNING traceback. A retired id is terminal: 4007 ``session_deleted``,
# no create attempt, no warning. Unexpected storage failures still warn.

MATERIALIZE_WARNING = "failed to materialize session row"


def _mint_prompt_delete(db, key=MINTED_KEY):
    """A minted key whose chat was persisted, then permanently deleted (the fence stays)."""
    db.record_minted_session_key(key)
    db.create_session(key, source="tui", model="test-model")
    db.append_message(key, "user", "scratch acceptance")
    db.delete_session(key)
    assert db.get_session(key) is None
    assert db.was_session_key_minted(key), "the mint record outlives the delete (the incident's state)"


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno >= 30 and r.name.startswith("tui_gateway")]


def test_resume_deleted_minted_key_is_terminal_not_a_draft(real_db, monkeypatch, caplog):
    _mint_prompt_delete(real_db)
    creates = []
    real_create = real_db.create_session
    monkeypatch.setattr(real_db, "create_session", lambda *a, **k: creates.append(a) or real_create(*a, **k))
    caplog.set_level("INFO")

    resp = _resume(session_id=MINTED_KEY)

    assert resp.get("error", {}).get("code") == 4007, resp
    assert (resp["error"].get("data") or {}).get("reason") == "session_deleted", resp
    assert creates == [], "a retired id must not reach the materializing create"
    assert real_db.get_session(MINTED_KEY) is None, "no resurrection"
    assert not _warnings(caplog), [r.getMessage() for r in _warnings(caplog)]
    assert any("retired" in r.getMessage() and MINTED_KEY in r.getMessage() for r in caplog.records), \
        "the retired-session skip is logged at INFO (detection guard)"


def test_resume_deletion_after_preflight_is_quiet_and_terminal(real_db, monkeypatch, caplog):
    """A delete landing between the retirement check and the create: the fence refuses the insert
    with RuntimeStoreError('not_found'); that known outcome is terminal and quiet, not a traceback."""
    real_db.record_minted_session_key(MINTED_KEY)
    real_create = real_db.create_session

    def racing_create(sid, *a, **k):
        from hermes_state_mutation_retirement import RETIRED_PREFIX
        real_db._execute_write(lambda conn: conn.execute(
            "INSERT INTO state_meta (key, value) VALUES (?, '1')", (RETIRED_PREFIX + sid,)))
        return real_create(sid, *a, **k)

    monkeypatch.setattr(real_db, "create_session", racing_create)
    caplog.set_level("INFO")

    resp = _resume(session_id=MINTED_KEY)

    assert resp.get("error", {}).get("code") == 4007, resp
    assert (resp["error"].get("data") or {}).get("reason") == "session_deleted", resp
    assert real_db.get_session(MINTED_KEY) is None, "the fence still holds"
    assert not _warnings(caplog), [r.getMessage() for r in _warnings(caplog)]


def test_resume_unexpected_materialize_failure_still_warns(real_db, monkeypatch, caplog):
    """Control: a genuine storage failure keeps its warning diagnostics and is not called a deletion."""
    real_db.record_minted_session_key(MINTED_KEY)

    def broken(*_a, **_k):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(real_db, "create_session", broken)
    caplog.set_level("INFO")

    resp = _resume(session_id=MINTED_KEY)

    assert resp.get("error", {}).get("code") == 4007, resp
    assert (resp["error"].get("data") or {}).get("reason") != "session_deleted", resp
    assert any(MATERIALIZE_WARNING in r.getMessage() for r in _warnings(caplog))


def test_resume_retirement_checked_in_the_selected_profile_store(real_db, monkeypatch, tmp_path, caplog):
    """The fence read is the resumed profile's store: a retirement in the launch store does not block a
    valid draft in the profile store, and the profile store's own retirement does."""
    profile_db = SessionDB(db_path=tmp_path / "work-state.db")
    work_home = tmp_path / "profiles" / "work"
    work_home.mkdir(parents=True)
    monkeypatch.setattr(server, "_profile_home", lambda name: work_home if name else None)
    monkeypatch.setattr(server, "_profile_session_db", lambda home: (profile_db, False) if home else (real_db, False))
    try:
        # launch store deleted this id; the profile store holds a valid, never-persisted draft
        _mint_prompt_delete(real_db)
        profile_db.record_minted_session_key(MINTED_KEY)
        _assert_resumed_ok(_resume(session_id=MINTED_KEY, profile="work"), MINTED_KEY)
        assert profile_db.get_session(MINTED_KEY) is not None
        assert real_db.get_session(MINTED_KEY) is None

        other = "20261112_200520_1827ba"
        _mint_prompt_delete(profile_db, other)
        caplog.set_level("INFO")
        resp = _resume(session_id=other, profile="work")
        assert (resp.get("error", {}).get("data") or {}).get("reason") == "session_deleted", resp
        assert profile_db.get_session(other) is None
        assert not _warnings(caplog)
    finally:
        profile_db.close()


def test_resume_valid_draft_still_materializes_after_retirement_check(real_db):
    """Control: the retirement check leaves a minted, never-deleted draft recoverable."""
    real_db.record_minted_session_key(MINTED_KEY)
    _assert_resumed_ok(_resume(session_id=MINTED_KEY), MINTED_KEY)
    assert real_db.get_session(MINTED_KEY) is not None