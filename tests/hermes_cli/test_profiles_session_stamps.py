"""GET /api/profiles/sessions/stamps: activity stamps for sidebar rows the projects tree doesn't carry. [daveey]"""

import os

import pytest

from hermes_cli import turn_session_activity as tsa


def _holder(pid, sid):
    return f"pid={pid}:turn={sid}:{sid}:abcd1234:platform=desktop"


@pytest.fixture
def rest(monkeypatch, _isolate_hermes_home):
    starlette = pytest.importorskip("starlette.testclient")
    import hermes_state
    from hermes_cli import profiles
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: home / "profiles")
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", home / "state.db")
    db = SessionDB(db_path=home / "state.db")
    try:
        for sid in ("parked_chat", "busy_chat"):
            db.create_session(sid, source="desktop")
            db.append_message(sid, role="user", content="go")
            db.append_message(sid, role="assistant", content="done")
        db._execute_write(lambda conn: conn.execute(
            "UPDATE sessions SET cwd = ? WHERE id = ?", ("/Users/x/code/infra", "parked_chat")))
        assert db.try_acquire_session_turn_lease("busy_chat", _holder(os.getpid(), "busy_chat"), ttl_seconds=600)
    finally:
        db.close()

    seen = []

    def fake_kanban(rows, *, display=False):
        assert display is True  # display endpoints explicitly opt in; strict defaults stay exact
        # The real annotator reads the boards; this one proves the endpoint feeds it rows with their lineage.
        for row in rows:
            seen.append((row["id"], list(row.get("_lineage_ids") or [])))
            if row["id"] == "parked_chat":
                row.update(kanban_active=5, kanban_parked=5, kanban_blocked=0, kanban_observed_at=123.0)
        return rows

    from hermes_cli.web_routers import profiles as profiles_router

    monkeypatch.setattr(profiles_router, "annotate_kanban_activity", fake_kanban)
    tsa._reset_cache_for_tests()
    c = starlette.TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    c.seen = seen
    yield c
    tsa._reset_cache_for_tests()


def test_stamps_carry_kanban_cwd_and_turn_live_for_named_ids(rest):
    body = rest.get("/api/profiles/sessions/stamps?profile=default&ids=parked_chat,busy_chat,missing").json()
    rows = {row["id"]: row for row in body["sessions"]}

    assert set(rows) == {"parked_chat", "busy_chat"}  # an unknown id is omitted, not an error
    assert rows["parked_chat"] == {
        **rows["parked_chat"],
        "kanban_active": 5,
        "kanban_parked": 5,
        "kanban_observed_at": 123.0,
        "cwd": "/Users/x/code/infra",
        "profile": "default",
    }
    assert rows["busy_chat"].get("turn_live") is True
    assert ("parked_chat", ["parked_chat"]) in rest.seen
    # Stamps only: no display/list fields that would overwrite the sidebar row's own.
    for row in rows.values():
        assert not {"title", "message_count", "started_at", "is_active", "system_prompt"} & set(row)


def test_unknown_profile_is_404(rest):
    assert rest.get("/api/profiles/sessions/stamps?profile=nope&ids=a").status_code == 404
