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
