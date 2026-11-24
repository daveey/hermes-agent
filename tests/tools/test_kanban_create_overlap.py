"""kanban_create refuses a second owner for work an open card already does. [daveey]

Incident (2026-10-28): a chat said "approve stage b" and created "Stage B: ..." on the default board, while
Stage B was already running on board ``unconventional-collab`` from another chat. The chat counted owners by
its OWN chat links, which held only the finished Stage A cards, and kanban_create created the duplicate with
no hint. The create now scans open cards on every board of this gateway (and the hermes-hub replica for other
gateways) and refuses on a match, naming the existing card; ``distinct_from`` + ``distinct_reason`` overrides
and is recorded as a ``create_overlap`` event.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

B1_TITLE = ("Stage B1: run the frozen X/T tuning + confirmation matrix and external-anchor rescoring "
            "on rented GPUs")
DUP_TITLE = "Stage B: GPU calibration, bounded comparison campaign and verified report"
DUP_BODY = ("David explicitly approved: 'approve stage b'. Execute frozen Stage B for "
            "Metta-AI/unconventional-collab. Checked this turn: current PLAN.md read via gh.")


def register_chat(home, session="20261123_120000_overlap_caller"):
    """Persist the human caller, not merely a plausible-looking session ID."""
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(session, source="desktop")
    finally:
        db.close()
    return session


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_capability_registry as reg
    monkeypatch.setattr(reg, "self_gateway", lambda: "hub")
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    monkeypatch.setenv("HERMES_SESSION_ID", register_chat(h))
    return h


def _board_with_b1(status_after_create=None):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_boards as kbb
    from hermes_cli import kanban_db_connect as kbc
    kbb.create_board("unconventional-collab")
    conn = kbc.connect(board="unconventional-collab")
    try:
        tid = kb.create_task(conn, title=B1_TITLE, body="Stage B / B1 of 3 on rented GPUs.",
                             assignee="default", session_id="20261026_154948_ea6cfd")
        if status_after_create:
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status_after_create, tid))
            conn.commit()
    finally:
        conn.close()
    return tid


def _default_titles():
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        return [t.title for t in kb.list_tasks(conn)]
    finally:
        conn.close()


def _create(**args):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_create({"title": DUP_TITLE, "body": DUP_BODY, "assignee": "default",
                                         "requires": [], **args}))


def test_stage_b_duplicate_on_another_board_is_refused_naming_the_owner(home):
    b1 = _board_with_b1()
    out = _create()
    assert out.get("ok") is False, out
    assert out.get("code") == "create_overlap"
    assert out.get("created") is False
    hit = {m["id"]: m for m in out["matches"]}
    assert b1 in hit
    assert hit[b1]["board"] == "unconventional-collab"
    assert "20261026_154948_ea6cfd" in hit[b1]["chats"]
    assert DUP_TITLE not in _default_titles()


def test_distinct_from_creates_and_records_the_override(home):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    b1 = _board_with_b1()
    no_reason = _create(distinct_from=[b1])
    assert no_reason.get("ok") is False and DUP_TITLE not in _default_titles()
    out = _create(distinct_from=[b1], distinct_reason="Stage B2 is a separate campaign from B1")
    assert out.get("ok") is True, out
    conn = kbc.connect()
    try:
        events = [e for e in kb.list_events(conn, out["task_id"]) if e.kind == "create_overlap"]
    finally:
        conn.close()
    assert len(events) == 1
    assert events[0].payload["outcome"] == "override"
    assert events[0].payload["reason"] == "Stage B2 is a separate campaign from B1"
    assert b1 in [m["id"] for m in events[0].payload["matches"]]


@pytest.mark.parametrize("status", ["done", "archived"])
def test_closed_cards_never_match(home, status):
    _board_with_b1(status_after_create=status)
    out = _create()
    assert out.get("ok") is True, out


def test_similar_title_on_the_same_board_matches(home):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="Retire the ci-gw runner boxes after the ASG cutover",
                             assignee="default", session_id="other-chat")
    finally:
        conn.close()
    out = _create(title="ASG cutover: retire ci-gw runner boxes", body="")
    assert out.get("code") == "create_overlap", out
    assert [m["id"] for m in out["matches"]] == [tid]
    unrelated = _create(title="Rotate the Telnyx API key", body="")
    assert unrelated.get("ok") is True, unrelated


def test_same_chat_fan_out_and_parents_are_not_matches(home):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="Retire the ci-gw runner boxes after the ASG cutover",
                                assignee="default", session_id="other-chat")
    finally:
        conn.close()
    out = _create(title="Retire ci-gw runner boxes: ASG cutover follow-up", body="", parents=[parent])
    assert out.get("ok") is True, out


def test_overlap_check_off_by_config(home):
    (home / "config.yaml").write_text("kanban:\n  create_overlap_check: false\n")
    _board_with_b1()
    assert _create().get("ok") is True


def _fake_replica(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE replica (gw TEXT, coll TEXT, key TEXT, data TEXT)")
    for gw, task in rows:
        db.execute("INSERT INTO replica VALUES (?,?,?,?)",
                   (gw, "tasks", f"default/{task['board']}/{task['id']}", json.dumps(task)))
    db.commit()
    db.close()


def test_placed_create_checks_the_target_gateways_replica_first(home, tmp_path, monkeypatch):
    """A MacBook chat whose card routes to the hub: the hub's open Stage B card is in the hub replica."""
    from hermes_cli import kanban_capability_registry as reg
    from tools import kanban_tools as kt
    monkeypatch.setattr(reg, "self_gateway", lambda: "macbook")
    _fake_replica(tmp_path / "hub-state" / "hub.db", [
        ("hub", {"id": "t_b1", "board": "unconventional-collab", "title": B1_TITLE, "status": "scheduled",
                 "gateway": "hub", "sessions": [{"session": "20261026_154948_ea6cfd"}]}),
        ("hub", {"id": "t_old", "board": "unconventional-collab", "title": "Stage A3", "status": "done",
                 "gateway": "hub"}),
    ])
    placed = []
    monkeypatch.setattr(kt, "_place_remote", lambda *a, **k: placed.append(a) or (None, None))
    out = _create()
    assert out.get("code") == "create_overlap", out
    assert [(m["gateway"], m["id"]) for m in out["matches"]] == [("hub", "t_b1")]
    assert placed == [] and DUP_TITLE not in _default_titles()


def test_missing_replica_never_blocks(home, monkeypatch):
    from hermes_cli import kanban_capability_registry as reg
    monkeypatch.setattr(reg, "self_gateway", lambda: "macbook")
    assert _create().get("ok") is True


def test_a_board_named_in_passing_with_an_unrelated_title_is_not_a_match(home):
    _board_with_b1()
    out = _create(title="Rotate the Telnyx API key",
                  body="Logs are under ~/.hermes/kanban/boards/unconventional-collab/ for reference.")
    assert out.get("ok") is True, out


# Mini bb_stall, 2026-10-28 20:42-21:28 PDT: every ci-runners alarm create exited 2.
# stall.log: "rc=2 from EC2 — the new card names board 'runners'". The alarm title is
# "CI: bb-linux runners can't run jobs (...)" and open hub/runners cards share the word "bb"
# (t_625bc2b7 ends "from EC2"). A plain-word board slug in the title is not naming that board.
ALARM_TITLE = ("CI: bb-linux runners can't run jobs "
               "(1 bb-linux job(s) queued > 30 min (oldest 0.5 h))")
ALARM_BODY = (
    "The merge train and every bb-linux CI job stall until a runner can start jobs.\n"
    "1 bb-linux job(s) queued > 30 min (oldest 0.5 h).\n"
    "Runners: metta1 online, 48 GB free.\n"
    "Disk full: free space or add a runner (tools/ci-runner/install-linux.sh).")
RUNNERS_TITLE = ("Runners L: real end-to-end natural-language macOS routing, "
                 "workspace build and BB action from EC2")


def _runners_owner():
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_boards as kbb
    from hermes_cli import kanban_db_connect as kbc
    kbb.create_board("runners")
    conn = kbc.connect(board="runners")
    try:
        return kb.create_task(conn, title=RUNNERS_TITLE, assignee="default",
                              session_id="other-chat")
    finally:
        conn.close()


def test_plain_word_board_slug_in_the_title_does_not_name_that_board(home):
    owner = _runners_owner()
    out = _create(title=ALARM_TITLE, body=ALARM_BODY)
    assert out.get("code") != "create_overlap", out
    assert out.get("ok") is True, out
    assert owner not in [m.get("id") for m in out.get("matches") or []]


def test_bare_plain_word_in_the_body_does_not_name_that_board(home):
    """'runners' or 'hermes' as an ordinary word, with no /slug or 'board slug', is not a reference."""
    _runners_owner()
    out = _create(title="CI: bb-linux fleet is down",
                  body="The runners are fine and hermes logs are unrelated. The bb fleet disk is full.")
    assert out.get("ok") is True, out


def test_explicit_board_reference_still_names_the_board(home):
    # Titles share 4 words (bb, macos, workspace, build) but not half of them: only the board reference
    # matches. One shared word ("bb") is not enough for a plain-word board (hub t_01a189b2d186a986).
    owner = _runners_owner()
    out = _create(title="CI: bb macOS workspace build is stuck",
                  body="See hub/runners. The bb fleet is stuck.")
    assert out.get("code") == "create_overlap", out
    assert owner in [m["id"] for m in out["matches"]]
    named = _create(title="CI: bb macOS workspace build on the runners board is stuck", body="The bb fleet is stuck.")
    assert named.get("code") == "create_overlap", named
    assert owner in [m["id"] for m in named["matches"]]
    one_word = _create(title="CI: bb-linux fleet is down", body="See hub/runners. The bb fleet is stuck.")
    assert one_word.get("ok") is True, one_word


def test_hyphenated_slug_in_the_title_still_names_the_board(home):
    owner = _board_with_b1()
    out = _create(title="external-anchor rescoring on unconventional-collab",
                  body="notes, no slash path and no 'board' marker")
    assert out.get("code") == "create_overlap", out
    assert owner in [m["id"] for m in out["matches"]]


def test_cli_alarm_create_is_not_refused_for_a_plain_word_board_slug(home, monkeypatch, capsys):
    """The mini alarm files via `hermes kanban create`, the path that exited 2."""
    import argparse
    from hermes_cli import kanban as kc
    _runners_owner()
    monkeypatch.setattr(kc, "_check_dispatcher_presence", lambda **kw: (True, ""))
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    # --board is a group flag pinned before the verb (alarm: `kanban --board default create`).
    # Omitting it uses the current board, which this fixture's create also targets.
    rc = kc._cmd_create(root.parse_args([
        "kanban", "create", ALARM_TITLE, "--body", ALARM_BODY,
        "--assignee", "default", "--json"]))
    captured = capsys.readouterr()
    assert rc == 0, (rc, captured.out, captured.err)
    assert "create_overlap" not in captured.err


def test_rest_alarm_create_is_not_refused_for_a_plain_word_board_slug(home):
    """Dashboard POST /tasks uses the same overlap check as the tool and the CLI."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from plugins.kanban.dashboard.plugin_api import router
    _runners_owner()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        r = client.post("/tasks", json={"title": ALARM_TITLE, "body": ALARM_BODY, "assignee": "default"})
    assert r.status_code != 409, r.text
    assert r.status_code == 200, r.text
    assert "create_overlap" not in r.text


# --- durable refusal record: `why` in the event payload, a JSONL line when no board can hold the event ---

def _overlap_events(board, task_id):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing(board=board) as conn:
        return [e.payload for e in kb.list_events(conn, task_id) if e.kind == "create_overlap"]


def _log_lines():
    from hermes_cli import kanban_overlap
    path = kanban_overlap.log_path()
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _remote_only(home, tmp_path, monkeypatch):
    """Self is the macbook; the only matching card lives on the hub replica."""
    from hermes_cli import kanban_capability_registry as reg
    monkeypatch.setattr(reg, "self_gateway", lambda: "macbook")
    _fake_replica(tmp_path / "hub-state" / "hub.db", [
        ("hub", {"id": "t_b1", "board": "unconventional-collab", "title": B1_TITLE, "status": "scheduled",
                 "gateway": "hub", "sessions": [{"session": "20261026_154948_ea6cfd"}]})])


def _cli_create(monkeypatch):
    import argparse
    from hermes_cli import kanban as kc
    monkeypatch.setattr(kc, "_check_dispatcher_presence", lambda **kw: (True, ""))
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    return kc._cmd_create(root.parse_args(["kanban", "create", DUP_TITLE, "--body", DUP_BODY,
                                           "--assignee", "default", "--json"]))


def _rest_create():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from plugins.kanban.dashboard.plugin_api import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        return client.post("/tasks", json={"title": DUP_TITLE, "body": DUP_BODY, "assignee": "default"})


def _assert_local_event_has_why(b1):
    [payload] = _overlap_events("unconventional-collab", b1)
    assert payload["outcome"] == "refused"
    [m] = payload["matches"]
    assert m["id"] == b1 and "the new card names board 'unconventional-collab'" in m["why"], m
    assert _log_lines() == []  # a DB event exists, so no second record


def _assert_remote_line_has_why():
    [line] = _log_lines()
    assert line["outcome"] == "refused" and line["title"] == DUP_TITLE and line["ts"] > 0
    [m] = line["matches"]
    assert (m["gateway"], m["id"]) == ("hub", "t_b1") and "unconventional-collab" in m["why"], m


def test_tool_local_refusal_event_carries_why(home):
    b1 = _board_with_b1()
    assert _create().get("code") == "create_overlap"
    _assert_local_event_has_why(b1)


def test_tool_all_remote_refusal_is_recorded_durably(home, tmp_path, monkeypatch):
    _remote_only(home, tmp_path, monkeypatch)
    assert _create().get("code") == "create_overlap"
    _assert_remote_line_has_why()


def test_cli_local_refusal_event_carries_why(home, monkeypatch, capsys):
    b1 = _board_with_b1()
    assert _cli_create(monkeypatch) == 2
    _assert_local_event_has_why(b1)


def test_cli_all_remote_refusal_is_recorded_durably(home, tmp_path, monkeypatch, capsys):
    _remote_only(home, tmp_path, monkeypatch)
    assert _cli_create(monkeypatch) == 2
    _assert_remote_line_has_why()


def test_rest_local_refusal_event_carries_why(home):
    b1 = _board_with_b1()
    assert _rest_create().status_code == 409
    _assert_local_event_has_why(b1)


def test_rest_all_remote_refusal_is_recorded_durably(home, tmp_path, monkeypatch):
    _remote_only(home, tmp_path, monkeypatch)
    assert _rest_create().status_code == 409
    _assert_remote_line_has_why()


def test_why_is_bounded_and_the_log_rotates(home, monkeypatch):
    from hermes_cli import kanban_overlap as ko
    monkeypatch.setattr(ko, "LOG_MAX_BYTES", 400)
    match = {"id": "t_x", "board": "b", "gateway": "hub", "status": "ready", "why": "w" * 5000, "_local": False}
    for _ in range(4):
        ko.record(None, None, outcome="refused", title="t", matches=[match])
    assert all(len(m["why"]) == ko.MAX_WHY for line in _log_lines() for m in line["matches"])
    rotated = ko.log_path().with_name(ko.LOG_NAME + ".1")
    assert rotated.exists() and ko.log_path().stat().st_size < 2000
