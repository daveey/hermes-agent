"""Local board obligations and plugin closeouts share board-qualified card identity.

The chat-closeout plugin keys a local card ``<board>/<task id>`` (its ``core.card_key`` over a
``session_tasks`` row: ``f"{card.get('board') or 'default'}/{card['id']}"``). Core counts the same card from the
board read under its bare id. A pending closeout for a card core already counts must not add a second card.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_session_activity as ksa

HUMAN_REQUEST = {
    "action": "Choose a release window.", "why_human": "Only the owner can choose downtime.",
    "interaction": "reply", "scope": "Staging release", "evidence": "No release window selected.",
}


def _plugin_card_key(card):
    """chat-closeout ``core.card_key`` for a local (non-remote) card, verbatim."""
    return f"{card.get('board') or 'default'}/{card['id']}"


def _board(path: Path, rows):
    """rows: (id, status, session_id, block_kind, human_request)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, session_id TEXT, "
                     "block_kind TEXT, human_request TEXT)")
        conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?)", [
            (tid, st, sid, kind, json.dumps(HUMAN_REQUEST) if human else None)
            for tid, st, sid, kind, human in rows])
    return path


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))  # never the real hermes-hub replica
    monkeypatch.setattr(ksa, "_waiting_sources", {})
    monkeypatch.setattr(ksa, "_remote_wait", lambda card: None)
    ksa._reset_cache_for_tests()
    yield tmp_path
    ksa._reset_cache_for_tests()


def _keys_for(sids, task_id):
    """The plugin's closeout key for ``task_id``, built from core's real ``session_tasks`` row."""
    rows = [c for c in ksa.session_tasks(sids, ksa.LISTED_STATUSES) if c["id"] == task_id]
    assert rows, f"{task_id} not listed by session_tasks"
    return {_plugin_card_key(c) for c in rows}


@pytest.mark.parametrize("status,kind,human,active,blocked", [
    ("running", None, False, 1, 0),
    ("review", None, False, 1, 0),
    ("blocked", "needs_input", True, 0, 1),
    ("triage", "capability", True, 0, 1),
])
@pytest.mark.parametrize("unrelated", [False, True])
def test_local_closeout_overlap_counts_once(root, status, kind, human, active, blocked, unrelated):
    _board(root / "kanban.db", [
        ("t_shared", status, "s_root", kind, human),
        ("t_done", "done", "s_root", None, False),
    ])
    shared = _keys_for(["s_root", "s_tip"], "t_shared")
    assert shared == {"default/t_shared"}
    pending = sorted(shared | ({"default/t_done"} if unrelated else set()))
    ksa.register_waiting_source("chat-closeout", lambda: {"s_tip": pending})
    for _ in range(2):  # fresh and cached annotation, across a compression lineage
        row = ksa.annotate_kanban_activity([{"id": "s_tip", "_lineage_ids": ["s_root", "s_tip"]}])[0]
        assert row.get("kanban_active", 0) == active + int(unrelated)
        assert row.get("kanban_blocked", 0) == blocked
        # One entry per card: the counted card keeps core's bare id, the unrelated closeout its scoped key.
        assert row["kanban_waiting_on"] == sorted({"t_shared"} | ({"default/t_done"} if unrelated else set()))
    # Existing API: bare local task ids, untouched by plugin sources.
    assert ksa.waiting_on_task_ids() == {"s_root": frozenset({"t_shared"})}


def test_same_task_id_on_another_board_is_not_conflated(root):
    _board(root / "kanban.db", [("t_x", "running", "s", None, False)])
    _board(root / "kanban" / "boards" / "ops" / "kanban.db", [("t_x", "done", "s", None, False)])
    assert _keys_for(["s"], "t_x") == {"default/t_x"}  # only the open card is listed
    ksa.register_waiting_source("chat-closeout", lambda: {"s": ["ops/t_x"]})
    row = ksa.annotate_kanban_activity([{"id": "s"}])[0]
    assert row["kanban_active"] == 2
    assert row["kanban_waiting_on"] == ["ops/t_x", "t_x"]


def test_named_board_overlap_counts_once(root):
    _board(root / "kanban" / "boards" / "ops" / "kanban.db", [("t_y", "blocked", "s", "needs_input", True)])
    assert _keys_for(["s"], "t_y") == {"ops/t_y"}
    ksa.register_waiting_source("chat-closeout", lambda: {"s": ["ops/t_y"]})
    row = ksa.annotate_kanban_activity([{"id": "s"}])[0]
    assert row.get("kanban_active", 0) == 0 and row["kanban_blocked"] == 1
    assert row["kanban_waiting_on"] == ["t_y"]


def test_legacy_bare_closeout_id_still_deduplicated(root):
    _board(root / "kanban.db", [("t_z", "running", "s", None, False)])
    ksa.register_waiting_source("chat-closeout", lambda: {"s": ["t_z"]})
    row = ksa.annotate_kanban_activity([{"id": "s"}])[0]
    assert row["kanban_active"] == 1 and row["kanban_waiting_on"] == ["t_z"]


def test_local_and_remote_overlaps_together(root, monkeypatch):
    _board(root / "kanban.db", [("t_l", "blocked", "s", "needs_input", True)])
    remote = "hub/default/default/t_l"  # same task id, another gateway: a different card
    monkeypatch.setattr(ksa, "_remote_cards", lambda *a: [{
        "key": remote, "id": "t_l", "session": "s", "status": "running",
        "gateway": "hub", "board": "default", "profile": "default", "unfinished": True}])
    ksa.register_waiting_source("chat-closeout", lambda: {"s": ["default/t_l", remote, "hub/default/default/t_gone"]})
    row = ksa.annotate_kanban_activity([{"id": "s"}])[0]
    assert row["kanban_blocked"] == 1           # local, counted once
    assert row["kanban_active"] == 2            # remote counted once + the unrelated remote closeout
    assert row["kanban_waiting_on"] == sorted(["t_l", remote, "hub/default/default/t_gone"])
    assert ksa.waiting_on_task_ids() == {"s": frozenset({"t_l"})}


def test_lineage_dedup_keeps_same_id_on_two_boards(root):
    _board(root / "kanban.db", [("t_x", "running", "s_root", None, False)])
    _board(root / "kanban" / "boards" / "ops" / "kanban.db", [("t_x", "running", "s_tip", None, False)])
    _board(root / "kanban" / "boards" / "bb" / "kanban.db", [
        ("t_y", "running", "s_root", None, False), ("t_y2", "blocked", "s_tip", "needs_input", True)])
    row = ksa.annotate_kanban_activity([{"id": "s_tip", "_lineage_ids": ["s_root", "s_tip"]}])[0]
    assert row["kanban_active"] == 3 and row["kanban_blocked"] == 1
