"""Existing-task adoption preserves owner state and reuses receipt publication."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli import kanban_origin_creation as creation
from hermes_cli.kanban_origin import OriginError


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    # Courier creation requires genuine renewal authority, which exists only on a synced board.
    (tmp_path / "config.yaml").write_text("kanban:\n  sync:\n    enabled: true\n")
    kb.init_db()
    with connect_closing() as conn:
        tid = kb.create_task(conn, title="Existing", session_id="creator")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done',completed_at=123,result='original' WHERE id=?", (tid,))
        yield conn, tid


def intent(tid):
    return {"request_id": "adoption-request-001",
            "owner": {"gateway": "worker", "profile": "default", "board": "default", "task_id": tid},
            "origin": {"gateway": "origin", "profile": "work", "session": "s_old"}}


def test_adopt_done_task_without_creating_or_changing_owner_state(board):
    conn, tid = board
    before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    assert creation.adopt_with_origin(conn, intent(tid), board="default") == tid
    row = conn.execute("SELECT * FROM origin_creation_intents").fetchone()
    marker = json.loads(row["binding_json"])
    assert marker["owner"] == intent(tid)["owner"]
    assert marker["origin"] == intent(tid)["origin"]
    assert marker["task_record_key"] == f"default/default/{tid}"
    assert "publication_version" not in marker
    assert dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == before
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    # The only new event is the synced copy of the intent row (kanban_origin_intent_carry): no lifecycle event.
    assert [r[0] for r in conn.execute("SELECT kind FROM task_events ORDER BY id")][events:] == [
        "origin_intent_recorded"]
    assert creation.adopt_with_origin(conn, intent(tid), board="default") == tid
    assert dict(conn.execute("SELECT * FROM origin_creation_intents").fetchone()) == dict(row)


@pytest.mark.parametrize("change,code", [
    ({"request_id": "bad"}, "origin_adoption_invalid"),
    ({"origin": {}}, "origin_adoption_invalid"),
    ({"extra": True}, "origin_adoption_invalid"),
])
def test_invalid_adoption_does_not_write(board, change, code):
    conn, tid = board
    with pytest.raises(OriginError) as exc:
        creation.adopt_with_origin(conn, {**intent(tid), **change}, board="default")
    assert exc.value.code == code
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='origin_creation_intents'").fetchone()


def test_exact_task_and_board_required(board):
    conn, tid = board
    for owner, code in [
        ({**intent(tid)["owner"], "board": "other"}, "origin_adoption_invalid"),
        ({**intent(tid)["owner"], "task_id": tid[:-1]}, "origin_task_missing"),
    ]:
        with pytest.raises(OriginError) as exc:
            creation.adopt_with_origin(conn, {**intent(tid), "owner": owner}, board="default")
        assert exc.value.code == code
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='origin_creation_intents'").fetchone()


def test_retry_archive_and_conflicting_request(board):
    conn, tid = board
    creation.adopt_with_origin(conn, intent(tid), board="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (tid,))
    assert creation.adopt_with_origin(conn, intent(tid), board="default") == tid
    changed = {**intent(tid), "origin": {**intent(tid)["origin"], "session": "other"}}
    with pytest.raises(OriginError) as exc:
        creation.adopt_with_origin(conn, changed, board="default")
    assert exc.value.code == "origin_creation_conflict"
    with pytest.raises(OriginError) as exc:
        creation.adopt_with_origin(conn, {**intent(tid), "request_id": "different-request"}, board="default")
    assert exc.value.code == "origin_already_bound"
    assert "adoption-request-001" in str(exc.value)
    assert conn.execute("SELECT COUNT(*) FROM origin_creation_intents").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "archived"


def test_changed_retry_to_missing_task_is_an_immutable_conflict(board):
    conn, tid = board
    creation.adopt_with_origin(conn, intent(tid), board="default")
    changed = {**intent(tid), "owner": {**intent(tid)["owner"], "task_id": "t_missing"}}
    with pytest.raises(OriginError) as exc:
        creation.adopt_with_origin(conn, changed, board="default")
    assert exc.value.code == "origin_creation_conflict"
    assert conn.execute("SELECT COUNT(*) FROM origin_creation_intents").fetchone()[0] == 1


def test_failed_adoption_insert_rolls_back_without_changing_task(board):
    import sqlite3
    conn, tid = board
    before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    def deny(action, table, *args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == "origin_creation_intents" else sqlite3.SQLITE_OK
    conn.set_authorizer(deny)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            creation.adopt_with_origin(conn, intent(tid), board="default")
    finally:
        conn.set_authorizer(None)
    assert dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == before
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='origin_creation_intents'").fetchone()
    assert creation.adopt_with_origin(conn, intent(tid), board="default") == tid


def test_concurrent_adoption_replay_keeps_one_marker(board):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    conn, tid = board
    barrier = Barrier(2)
    def adopt():
        with connect_closing() as other:
            barrier.wait(timeout=10)
            return creation.adopt_with_origin(other, intent(tid), board="default")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(adopt) for _ in range(2)]
        assert [f.result(timeout=20) for f in futures] == [tid, tid]
    assert conn.execute("SELECT COUNT(*) FROM origin_creation_intents").fetchone()[0] == 1


@pytest.mark.parametrize("first", ["create", "adopt"])
def test_create_adopt_request_id_collision_preserves_original_operation(board, first):
    conn, tid = board
    adoption = intent(tid)
    creation_intent = {**adoption, "owner": {
        key: value for key, value in adoption["owner"].items() if key != "task_id"
    }}
    operations = {
        "create": lambda: kb.create_task(conn, title="Build", origin_intent=creation_intent),
        "adopt": lambda: creation.adopt_with_origin(conn, adoption, board="default"),
    }
    operations[first]()
    original = [dict(row) for row in conn.execute("SELECT * FROM origin_creation_intents")]
    tasks = [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")]
    events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    second = "adopt" if first == "create" else "create"
    with pytest.raises(OriginError) as exc:
        operations[second]()
    assert exc.value.code == "origin_creation_conflict"
    assert [dict(row) for row in conn.execute("SELECT * FROM origin_creation_intents")] == original
    assert [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")] == tasks
    assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == events


def test_registered_adoption_tool_requires_trusted_context(board):
    from tools import kanban_tools
    from tools.registry import registry
    conn, tid = board
    result = json.loads(registry.dispatch("kanban_origin_adopt", {"task_id": tid, "board": "default"},
                                         origin_intent=intent(tid)))
    assert result.get("adopted") is True, result
    assert result["created"] is False
    assert result["handoff_complete"] is False
    assert result["ok"] is False
    assert result["origin_creation"]["binding"]["owner"] == intent(tid)["owner"]
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    refused = json.loads(registry.dispatch("kanban_origin_adopt", {"task_id": tid, "board": "default"}))
    assert refused.get("code") == "origin_context_required", refused


# --- Refusal outside courier context names the supported CLI path [daveey] -------------------------------------

def _hub_state(tmp_path, monkeypatch, me, tasks=()):
    """A minimal hermes-hub state dir: this gateway's id plus replica rows (gw, profile/board/task)."""
    import sqlite3
    state = tmp_path / "hub-state"
    state.mkdir(exist_ok=True)
    with sqlite3.connect(state / "hub.db") as db:
        db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS replica (gw TEXT, coll TEXT, key TEXT, version INTEGER, seq INTEGER, "
                   "data TEXT, PRIMARY KEY (gw, coll, key))")
        db.execute("INSERT OR REPLACE INTO meta VALUES ('gateway', ?)", (me,))
        for gw in {me, *(t[0] for t in tasks)}:
            db.execute("INSERT OR REPLACE INTO replica (gw, coll, key, data) VALUES (?, 'gateway', 'info', '{}')", (gw,))
        for gw, key in tasks:
            db.execute("INSERT OR REPLACE INTO replica (gw, coll, key, data) VALUES (?, 'tasks', ?, '{}')", (gw, key))
    monkeypatch.setenv("HERMES_HUB_STATE", str(state))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    for name in ("HERMES_KANBAN_TASK", "HERMES_ORIGIN_SESSION", "HERMES_SESSION_ID", "HERMES_PROFILE_NAME"):
        monkeypatch.delenv(name, raising=False)


def _refuse(task_id, board="default"):
    from tools import kanban_tools  # registers the tool
    from tools.registry import registry
    return json.loads(registry.dispatch("kanban_origin_adopt", {"task_id": task_id, "board": board}))


def test_chat_refusal_names_exact_adopt_task_command(board, tmp_path, monkeypatch):
    _hub_state(tmp_path, monkeypatch, "macbook", [("hub", "default/default/t_remote1")])
    monkeypatch.setenv("HERMES_SESSION_ID", "20261025_234720_c405df")
    refused = _refuse("t_remote1")
    assert refused["code"] == "origin_context_required", refused
    assert refused["handoff_complete"] is False
    assert refused["next_step"] == ("hermes-hub adopt-task hub default/default/t_remote1 "
                                    "--origin-session 20261025_234720_c405df --origin-profile default "
                                    "--id adopt:t_remote1:20261025_234720_c405df")
    assert refused["run_on"] == "macbook"
    assert refused["complete"] is True
    assert "hermes-hub adopt-task" in refused["error"]


def test_chat_refusal_uses_owner_profile_and_board_from_replica(board, tmp_path, monkeypatch):
    _hub_state(tmp_path, monkeypatch, "macbook", [("mini", "bb-dev/bb/t_bb1"), ("hub", "default/default/t_other")])
    monkeypatch.setenv("HERMES_SESSION_ID", "s_chat")
    monkeypatch.setenv("HERMES_PROFILE", "work")
    refused = _refuse("t_bb1", board="bb")
    assert refused["next_step"] == ("hermes-hub adopt-task mini bb-dev/bb/t_bb1 --origin-session s_chat "
                                    "--origin-profile work --id adopt:t_bb1:s_chat")


def test_chat_refusal_with_unknown_owner_keeps_placeholders(board, tmp_path, monkeypatch):
    _hub_state(tmp_path, monkeypatch, "macbook")
    refused = _refuse("t_nowhere")
    assert refused["code"] == "origin_context_required"
    assert refused["next_step"].startswith("hermes-hub adopt-task <owner-gateway> <owner-profile>/default/t_nowhere ")
    assert "--origin-session <origin-session-id>" in refused["next_step"]
    assert refused["run_on"] == "macbook"
    assert refused["complete"] is False
    assert "not in this gateway's hermes-hub replica" in refused["error"]
    assert "Replace every <placeholder>" in refused["error"]


@pytest.mark.parametrize("task_id,board_slug,session", [
    ("t_x; touch /tmp/pwned", "default", "s_ok"),
    ("t_x", "de fault", "s_ok"),
    ("t_x", "default", "$(id)"),
    ("t_x", "default", "--help"),
])
def test_unsafe_values_become_placeholders_never_shell_text(board, tmp_path, monkeypatch, task_id, board_slug, session):
    _hub_state(tmp_path, monkeypatch, "macbook", [("hub", "default/default/t_x")])
    monkeypatch.setenv("HERMES_SESSION_ID", session)
    refused = _refuse(task_id, board=board_slug)
    assert refused["complete"] is False
    for bad in (";", "$(", " de fault", "--help", "touch"):
        assert bad not in refused["next_step"]


def test_chat_refusal_for_card_on_this_gateway_subscribes_instead(board, tmp_path, monkeypatch):
    _conn, tid = board
    _hub_state(tmp_path, monkeypatch, "hub", [("hub", f"default/default/{tid}")])
    monkeypatch.setenv("HERMES_SESSION_ID", "s_local")
    refused = _refuse(tid)
    assert refused["next_step"] == f"hermes kanban --board default origin-adopt {tid} --local"
    assert refused["run_on"] == "hub"


def test_chat_refusal_for_card_on_another_local_profile_names_it(board, tmp_path, monkeypatch):
    _hub_state(tmp_path, monkeypatch, "hub", [("hub", "work/release/t_rel1")])
    monkeypatch.setenv("HERMES_SESSION_ID", "s_local")
    refused = _refuse("t_rel1", board="release")
    assert refused["next_step"] == (
        "hermes-hub adopt-task hub work/release/t_rel1 --origin-session s_local "
        "--origin-profile default --id adopt:t_rel1:s_local")
    assert refused["run_on"] == "hub"


def test_cross_profile_hint_executes_through_courier_owner_entry(board, tmp_path, monkeypatch):
    """Keep source profile provenance; never switch the chat into the target profile."""
    import shlex
    from tools.registry import registry
    conn, tid = board
    _hub_state(tmp_path, monkeypatch, "hub", [("hub", f"work/default/{tid}")])
    monkeypatch.setenv("HERMES_SESSION_ID", "s_local")
    words = shlex.split(_refuse(tid)["next_step"])
    assert words[:2] == ["hermes-hub", "adopt-task"]
    # The courier contract carries both profiles independently into the trusted
    # owner entrypoint; no local state.db lookup in the target profile is needed.
    profile, slug, task = words[3].split("/")
    request = {"request_id": words[words.index("--id") + 1],
               "owner": {"gateway": words[2], "profile": profile, "board": slug, "task_id": task},
               "origin": {"gateway": "hub", "profile": words[words.index("--origin-profile") + 1],
                          "session": words[words.index("--origin-session") + 1]}}
    monkeypatch.setenv("HERMES_PROFILE", "work")
    result = json.loads(registry.dispatch("kanban_origin_adopt", {"task_id": tid, "board": "default"},
                                         origin_intent=request))
    assert result["adopted"] is True, result
    binding = result["origin_creation"]["binding"]
    assert binding["owner"]["profile"] == "work"
    assert binding["origin"] == {"gateway": "hub", "profile": "default", "session": "s_local"}
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_worker_refusal_points_at_origin_gateway_cli(board, tmp_path, monkeypatch):
    _conn, tid = board
    _hub_state(tmp_path, monkeypatch, "hub", [("hub", f"default/default/{tid}"), ("macbook", "default/default/t_x")])
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_ORIGIN_SESSION", "@session:default@macbook/20261025_234720_c405df")
    refused = _refuse(tid)
    assert refused["next_step"] == (f"hermes-hub adopt-task hub default/default/{tid} "
                                    "--origin-session 20261025_234720_c405df --origin-profile default "
                                    f"--id adopt:{tid}:20261025_234720_c405df")
    assert refused["run_on"] == "macbook"
    assert refused["complete"] is True
    assert refused["delegate"].startswith("hermes-hub create-task macbook ")
    assert "--origin-session ''" in refused["delegate"]
    assert f"--expect hub/default/default/{tid}" in refused["delegate"]
    assert "kanban_origin_adopt" not in refused["next_step"]


def test_worker_refusal_carries_non_default_profiles(board, tmp_path, monkeypatch):
    """Owner card on profile bb-dev, origin chat on profile personal: both must survive into the command and the
    links check (a 3-part --expect ref means the default profile)."""
    _conn, tid = board
    _hub_state(tmp_path, monkeypatch, "mini", [("mini", f"bb-dev/default/{tid}"), ("macbook", "personal/default/t_y")])
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "bb-dev")
    monkeypatch.setenv("HERMES_ORIGIN_SESSION", "@session:personal@macbook/s_mac")
    refused = _refuse(tid)
    assert refused["next_step"] == (f"hermes-hub adopt-task mini bb-dev/default/{tid} --origin-session s_mac "
                                    f"--origin-profile personal --id adopt:{tid}:s_mac")
    assert f"--expect mini/bb-dev/default/{tid}" in refused["delegate"]


def test_worker_refusal_without_known_origin_names_placeholders(board, tmp_path, monkeypatch):
    _conn, tid = board
    _hub_state(tmp_path, monkeypatch, "hub", [("hub", f"default/default/{tid}")])
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    refused = _refuse(tid)
    assert refused["next_step"].startswith(f"hermes-hub adopt-task hub default/default/{tid} --origin-session <origin-session-id>")
    assert refused["run_on"] == "<origin-gateway>"
    assert refused["complete"] is False
    assert refused["delegate"].startswith("hermes-hub create-task <origin-gateway> ")


def test_worker_with_origin_on_same_gateway_gets_no_self_addressed_delegate(board, tmp_path, monkeypatch):
    _conn, tid = board
    _hub_state(tmp_path, monkeypatch, "hub", [("hub", f"default/default/{tid}")])
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_ORIGIN_SESSION", "@session:default@hub/s_here")
    refused = _refuse(tid)
    assert "delegate" not in refused
    assert refused["next_step"] == f"hermes kanban --board default origin-adopt {tid} --local"
    assert "cross_session" in refused["error"]


def test_chat_refusal_names_this_turns_chat_not_the_process_env(board, tmp_path, monkeypatch):
    """[daveey] agent-884fdd96f8: a gateway serving several chats keeps whichever chat wrote os.environ last in
    HERMES_SESSION_ID; the current turn's chat is the session ContextVar. The hint must bind the calling chat,
    or the agent subscribes an unrelated chat to its card (hub 2026-11-03, chat 5fb607 got 370c8b's cards)."""
    import contextvars
    import gateway.session_context as sc
    _conn, tid = board
    _hub_state(tmp_path, monkeypatch, "hub", [("hub", f"default/default/{tid}")])
    monkeypatch.setenv("HERMES_SESSION_ID", "s_other_chat")  # the chat that ran last in this process

    def turn():
        tokens = sc.set_session_vars(session_id="s_this_chat", session_key="s_this_chat", source="desktop")
        try:
            return _refuse(tid)
        finally:
            sc.clear_session_vars(tokens)

    refused = contextvars.copy_context().run(turn)
    assert refused["next_step"] == f"hermes kanban --board default origin-adopt {tid} --local", refused
