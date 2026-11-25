"""Local card identity survives aggregation across compression aliases."""

import json
import sqlite3

import pytest

from hermes_cli import kanban_session_activity as ksa


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    monkeypatch.setattr(ksa, "_remote_cards", lambda *args: [])
    ksa._reset_cache_for_tests()
    path = tmp_path / "kanban.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, session_id TEXT, human_request TEXT)")
        conn.execute("CREATE TABLE kanban_notify_subs "
                     "(task_id TEXT, platform TEXT, chat_id TEXT, delivery_metadata TEXT)")
    yield path
    ksa._reset_cache_for_tests()


def _add_card(path, tid, status, creator, subscriber=None, reason="shepherd"):
    request = {
        "action": "Choose a release window.", "why_human": "Only the owner can choose downtime.",
        "interaction": "reply", "scope": "Staging release", "evidence": "No release window selected.",
    } if status == "blocked" else None
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?)",
                     (tid, status, creator, json.dumps(request)))
        if subscriber:
            conn.execute("INSERT INTO kanban_notify_subs VALUES (?, 'tui', ?, ?)",
                         (tid, subscriber, json.dumps({"reason": reason})))


def _row():
    return {"id": "s_tip", "_lineage_ids": ["s_root", "s_tip"]}


@pytest.mark.parametrize("reason", ["create", "shepherd"])
@pytest.mark.parametrize("status,field,parked", [
    ("running", "kanban_active", 0),
    ("scheduled", "kanban_active", 1),
    ("blocked", "kanban_blocked", None),
])
def test_shared_local_card_counts_once_per_lineage(board, reason, status, field, parked):
    _add_card(board, "t_shared", status, "s_root", "s_tip", reason)
    assert len(ksa.session_tasks(["s_root", "s_tip"])) == 1
    for _ in range(2):  # fresh projection, then cached snapshot
        row = ksa.annotate_kanban_activity([_row()])[0]
        assert row[field] == 1
        assert row.get("kanban_parked") == parked
        assert row["kanban_waiting_on"] == ["t_shared"]
    # Each alias still owns the obligation when displayed alone.
    assert ksa.annotate_kanban_activity([{"id": "s_root"}])[0][field] == 1
    assert ksa.annotate_kanban_activity([{"id": "s_tip"}])[0][field] == 1


def test_distinct_local_cards_are_not_collapsed(board):
    _add_card(board, "t_root", "running", "s_root")
    _add_card(board, "t_tip", "running", "s_tip")
    assert len(ksa.session_tasks(["s_root", "s_tip"])) == 2
    assert ksa.annotate_kanban_activity([_row()])[0]["kanban_active"] == 2


def test_local_identity_snapshot_tracks_board_changes(board):
    _add_card(board, "t_shared", "running", "s_root", "s_tip")
    with ksa.one_annotation_snapshot():
        assert ksa.annotate_kanban_activity([_row()])[0]["kanban_active"] == 1
        with sqlite3.connect(board) as conn:
            conn.execute("UPDATE tasks SET status='archived'")
        assert ksa.annotate_kanban_activity([_row()])[0]["kanban_active"] == 1
    assert "kanban_active" not in ksa.annotate_kanban_activity([_row()])[0]


def test_shared_held_card_is_a_single_active_subset(board):
    _add_card(board, "t_parent", "blocked", "s_elsewhere")
    _add_card(board, "t_shared", "todo", "s_root", "s_tip")
    with sqlite3.connect(board) as conn:
        conn.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
        conn.execute("INSERT INTO task_links VALUES ('t_parent', 't_shared')")
    row = ksa.annotate_kanban_activity([_row()])[0]
    assert row["kanban_active"] == row["kanban_held"] == 1
    assert row["kanban_parked"] == 0


def test_shared_stalled_card_is_a_single_parked_subset(board, monkeypatch):
    _add_card(board, "t_shared", "scheduled", "s_root", "s_tip")
    with sqlite3.connect(board) as conn:
        conn.execute("CREATE TABLE task_events "
                     "(id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER)")
        conn.execute("INSERT INTO task_events VALUES (1, 't_shared', 'scheduled', '{}', 1)")
    monkeypatch.setattr(ksa.time, "time", lambda: 100000.0)
    row = ksa.annotate_kanban_activity([_row()])[0]
    assert row["kanban_active"] == row["kanban_parked"] == row["kanban_stalled"] == 1


@pytest.mark.parametrize("status", ["running", "scheduled"])
def test_mixed_local_and_remote_lineage_snapshot(board, monkeypatch, status):
    _add_card(board, "t_shared", "running", "s_root", "s_tip")
    remote = [{"key": "other/default/t_shared", "id": "t_shared", "status": status, "session": sid,
               "board": "default", "gateway": "other"} for sid in ("s_root", "s_tip")]
    monkeypatch.setattr(ksa, "_remote_cards", lambda *args: remote)
    monkeypatch.setattr(ksa, "_remote_wait", lambda card: {"wait_state": "stalled"} if status == "scheduled" else None)
    assert len(ksa.session_tasks(["s_root", "s_tip"])) == 2
    with ksa.one_annotation_snapshot():
        for _ in range(2):
            row = ksa.annotate_kanban_activity([_row()])[0]
            assert row["kanban_active"] == 2
            if status == "scheduled":
                assert row["kanban_parked"] == row["kanban_stalled"] == 1


def test_annotation_keeps_count_and_identity_generation_together(board, monkeypatch):
    _add_card(board, "t_shared", "running", "s_root", "s_tip")
    real_counts = ksa._all_counts

    def refresh_after_read(**kwargs):
        result = real_counts(**kwargs)
        with sqlite3.connect(board) as conn:
            conn.execute("UPDATE tasks SET status='archived'")
        real_counts(include_waiting=True)
        return result

    monkeypatch.setattr(ksa, "_all_counts", refresh_after_read)
    assert ksa.annotate_kanban_activity([_row()])[0]["kanban_active"] == 1


@pytest.mark.parametrize('sid', ['ordinary-user-chat', 'kanban_owner_terminal'])
@pytest.mark.parametrize('kind', ['scope_stopped', 'decision_pending'])
def test_administrative_triage_remains_unfinished_without_a_live_executor(board, monkeypatch, sid, kind):
    _add_card(board, 't_stuck', 'triage', sid)
    _add_card(board, 't_done', 'done', sid)
    with sqlite3.connect(board) as conn:
        conn.execute('CREATE TABLE task_events '
                     '(id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER)')
        conn.execute("INSERT INTO task_events VALUES (1, 't_stuck', 'decision_retained', ?, 1)",
                     (json.dumps({'disposition': {'reason': kind}}),))
    monkeypatch.setattr(ksa.time, 'time', lambda: 100000.0)
    row = ksa.annotate_kanban_activity([{'id': sid}])[0]
    assert row['kanban_active'] == row['kanban_parked'] == 1
    assert 'kanban_blocked' not in row and not row.get('needs_you')
    assert row['kanban_waiting_on'] == ['t_stuck']
    assert row['kanban_observed_at'] <= 100000.0
    assert {card['id'] for card in ksa.session_tasks([sid])} == {'t_stuck'}


def test_same_local_id_on_different_boards_remains_distinct(board):
    _add_card(board, "t_shared", "running", "s_root", "s_tip")
    other = board.parent / "kanban" / "boards" / "other" / "kanban.db"
    other.parent.mkdir(parents=True)
    with sqlite3.connect(board) as source, sqlite3.connect(other) as target:
        source.backup(target)
    assert len(ksa.session_tasks(["s_root", "s_tip"])) == 2
    assert ksa.annotate_kanban_activity([_row()])[0]["kanban_active"] == 2
