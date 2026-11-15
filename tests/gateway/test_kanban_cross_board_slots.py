"""Shared host slots go to the best card on ANY board, with a starvation guard. [daveey]

Regression (hermes-hub, 2026-11-12): ``kanban.max_in_progress`` (9) is one budget for every board, but the gateway
ticked boards one after another in a rotating order and each tick took every free slot for its own cards. Board
``hermes`` started two priority-0 cards at 05:08:50/52 while default-board t_01a1ac912db0cb5c (priority 51010) sat
ready from 04:40 reading "0 card(s) ahead of it, 9/9 worker slots busy"; chats raised it to 60320 for nothing.
"""
from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    kb.create_board("hermes")
    monkeypatch.setattr(kbd, "configured_max_in_progress", lambda: 1)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda sample=None: "ok")
    return root


def _card(board, title, priority, *, ready_since=None):
    with kbc.connect_closing(board=board) as conn:
        tid = kb.create_task(conn, title=title, assignee="default", priority=priority)
        if ready_since is not None:
            conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (int(ready_since), tid))
            conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ?", (int(ready_since), tid))
        conn.commit()
    return tid


def _running(board):
    """One worker really running on ``board``: the queue place counts it through that board's file (read-only)."""
    with kbc.connect_closing(board=board) as conn:
        busy = kb.create_task(conn, title="busy", assignee="default")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (busy,))
        conn.commit()


def _dispatcher(monkeypatch, spawned, *, start_board):
    from gateway.kanban_watchers_dispatcher import _DispatcherSettings, _KanbanDispatcher
    real = kbd.dispatch_once

    def spawn(task, workspace, board=None):
        spawned.append((board or "default", task.id))
        return 4242

    monkeypatch.setattr(kbd, "dispatch_once", lambda conn, **kw: real(conn, spawn_fn=spawn, **kw))
    dispatcher = _KanbanDispatcher(kb, _DispatcherSettings(60, None, 1, 2, 0, False, None, None))
    # Rotate the round-robin start so the board holding the WORSE card ticks first (the 05:08 order).
    slugs = list(dispatcher._board_slugs())
    dispatcher._rr_offset = slugs.index(start_board)
    return dispatcher


def test_one_shared_slot_goes_to_the_higher_priority_card_on_the_other_board(home, monkeypatch):
    high = _card("default", "default top card", 900)
    low = _card("hermes", "hermes p0 card", 0)
    spawned = []
    dispatcher = _dispatcher(monkeypatch, spawned, start_board="hermes")

    results = dispatcher.tick_once()

    assert spawned == [("default", high)], spawned
    with kbc.connect_closing(board="hermes") as conn:
        assert kb.get_task(conn, low).status == "ready"
    hermes = dict(results)["hermes"]
    assert hermes.at_capacity == "max_in_progress=1/1"  # ticked after default spent the slot


def test_zero_quota_refuses_spawns_and_says_why(home):
    with kbc.connect_closing(board="hermes") as conn:
        res = kbd.DispatchResult()
        assert kbd._tick_spawn_budget(conn, res, max_spawn=None, max_in_progress=5, board="hermes",
                                      spawn_quota=0) == (False, None)
        assert res.at_capacity == "board_share=0 (free slots go to higher-ranked cards on other boards sharing " \
                                  "these slots)"
        assert kbd._tick_spawn_budget(conn, kbd.DispatchResult(), max_spawn=None, max_in_progress=5,
                                      board="hermes", spawn_quota=2) == (True, 2)


def test_flooded_board_cannot_starve_another_board_forever(home, monkeypatch):
    from hermes_cli import kanban_board_share as share
    old = time.time() - share.STARVE_SECONDS - 60
    starved = _card("hermes", "hermes p0, ready past the starvation window", 0, ready_since=old)
    for i in range(3):
        _card("default", f"default flood {i}", 1000)
    spawned = []
    dispatcher = _dispatcher(monkeypatch, spawned, start_board="default")

    dispatcher.tick_once()

    assert spawned == [("hermes", starved)], spawned


def test_a_guarded_starved_head_does_not_win_its_board_every_first_pick(home, monkeypatch):
    """Reviewer probe (PR #1238): hermes' starved head is dispatch-guarded and never starts. Once hermes has been
    served (a worker started after the head became ready) it is not starved, so default's p1000 card takes the
    only slot, not hermes' p0 filler; the queue count no longer lists the guarded head either."""
    from hermes_cli import kanban_board_share as share
    from hermes_cli import kanban_model_assignment as kma
    from hermes_cli import kanban_queue_position as kqp
    old = time.time() - share.STARVE_SECONDS - 60
    guarded = _card("hermes", "hermes p500, guarded, starved", 500, ready_since=old)
    filler = _card("hermes", "hermes p0 filler", 0)
    top = _card("default", "default p1000 card", 1000)
    with kbc.connect_closing(board="hermes") as conn:  # hermes started a worker after the head became ready
        conn.execute("INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'done', ?)",
                     (filler, int(time.time() - 600)))
        conn.commit()
    real_guard = kma.dispatch_guard
    monkeypatch.setattr(kma, "dispatch_guard", lambda conn, task_id, **kw: (
        "test guard" if task_id == guarded else real_guard(conn, task_id, **kw)))
    spawned = []
    dispatcher = _dispatcher(monkeypatch, spawned, start_board="hermes")

    dispatcher.tick_once()

    assert spawned == [("default", top)], spawned
    with kbc.connect_closing(board="hermes") as conn:
        assert kb.get_task(conn, filler).status == "ready"
    mine = _card("default", "default p900 card", 900)
    _running("hermes")
    with kbc.connect_closing(board="default") as conn:
        place = kqp.position(conn, kb.get_task(conn, mine), "default")
    assert place is not None and "ahead_other_boards" not in place, place


def test_an_aged_card_below_the_priority_limit_wins_the_shared_slot(home, monkeypatch):
    """Cross-board share ranks by the dispatcher's aged admission score, scored before the per-board cut: hermes'
    p0 card has waited an hour (score 1500), below a fresh p100 card a priority-ordered ``LIMIT 1`` kept, and
    outranks default's fresh p900 card well inside :data:`STARVE_SECONDS` (t_01a1dca84c849edc)."""
    aged = _card("hermes", "hermes p0, waiting an hour", 0, ready_since=time.time() - 3600)
    _card("hermes", "hermes p100, fresh", 100)
    _card("default", "default p900, fresh", 900)
    spawned = []
    dispatcher = _dispatcher(monkeypatch, spawned, start_board="default")

    dispatcher.tick_once()

    assert spawned == [("hermes", aged)], spawned


def test_a_served_board_does_not_age_across_boards(home, monkeypatch):
    """The age floor is the board's latest worker start: hermes started a card 5 minutes ago, so its hour-old p0
    card counts 5 minutes (125) across boards and default's p900 card takes the slot."""
    _card("hermes", "hermes p0, waiting an hour", 0, ready_since=time.time() - 3600)
    other = _card("hermes", "hermes p100, fresh", 100)
    top = _card("default", "default p900, fresh", 900)
    with kbc.connect_closing(board="hermes") as conn:
        conn.execute("INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'done', ?)",
                     (other, int(time.time() - 300)))
        conn.commit()
    spawned = []
    dispatcher = _dispatcher(monkeypatch, spawned, start_board="hermes")

    dispatcher.tick_once()

    assert spawned == [("default", top)], spawned


def test_cross_board_ranking_falls_back_to_priority_when_the_score_is_unreadable(home, monkeypatch):
    import sqlite3

    from hermes_cli import kanban_admission_score as score
    from hermes_cli import kanban_board_share as share
    _card("hermes", "hermes p0, waiting an hour", 0, ready_since=time.time() - 3600)
    fresh = _card("hermes", "hermes p100, fresh", 100)

    def broken(*_a, **_kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(score, "rank", broken)
    with kbc.connect_closing(board="hermes") as conn:
        [head] = share.candidates(conn, 1)
    assert head.task_id == fresh and head.score is None and head.priority == 100


def test_board_started_nothing_keeps_the_starvation_pick(home):
    from hermes_cli import kanban_board_share as share
    old = time.time() - share.STARVE_SECONDS - 60
    head = _card("hermes", "hermes p0, starved", 0, ready_since=old)
    with kbc.connect_closing(board="hermes") as conn:  # a start BEFORE the head became ready does not count
        conn.execute("INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'done', ?)",
                     (head, int(old - 60)))
        conn.commit()
        assert share._starved(share.candidates(conn, 5), time.time())


def test_queue_text_counts_higher_ranked_cards_on_other_boards(home, monkeypatch):
    from hermes_cli import kanban_parent_unclaimed as pu
    from hermes_cli import kanban_queue_position as kqp
    _running("hermes")
    _card("hermes", "hermes higher card", 900)
    _card("hermes", "hermes lower card", 0)
    mine = _card("default", "default card", 500)
    with kbc.connect_closing(board="default") as conn:
        place = kqp.position(conn, kb.get_task(conn, mine), "default")
    assert place["ahead"] == 0 and place["ahead_other_boards"] == 1
    assert pu._queue_words(place) == (
        "1 card(s) ahead of it (1 on other boards sharing these slots), 1/1 worker slots busy")
    assert kqp.describe(place).startswith(
        "waiting for a local slot: ~1 ahead (1 on other boards sharing these slots), 1/1 running.")


def test_no_other_board_ahead_keeps_the_single_board_wording(home, monkeypatch):
    from hermes_cli import kanban_parent_unclaimed as pu
    from hermes_cli import kanban_queue_position as kqp
    _running("hermes")
    _card("hermes", "hermes lower card", 0)
    mine = _card("default", "default card", 500)
    with kbc.connect_closing(board="default") as conn:
        place = kqp.position(conn, kb.get_task(conn, mine), "default")
    assert "ahead_other_boards" not in place
    assert pu._queue_words(place) == "0 card(s) ahead of it, 1/1 worker slots busy"


def test_queue_count_includes_a_starved_head_on_another_board(home, monkeypatch):
    from hermes_cli import kanban_board_share as share
    from hermes_cli import kanban_queue_position as kqp
    _card("hermes", "hermes p0, starved", 0, ready_since=time.time() - share.STARVE_SECONDS - 60)
    mine = _card("default", "default card", 500)
    with kbc.connect_closing(board="default") as conn:
        place = kqp.position(conn, kb.get_task(conn, mine), "default")
    assert place is not None and place.get("ahead_other_boards") == 1


def test_plan_ranks_globally_and_returns_none_without_contention():
    from hermes_cli import kanban_board_share as share
    now = 1_000_000.0

    def cand(tid, prio, created=now - 10):
        return share.Candidate(tid, prio, False, created, created)

    heads = {"default": [cand("a1", 50), cand("a2", 1)], "hermes": [cand("b1", 10)]}
    assert share.plan(heads, 3, now=now) is None  # room for every card: each board keeps its own order
    assert share.plan(heads, 2, now=now) == {"default": 1, "hermes": 1}
    assert share.plan(heads, 1, now=now) == {"default": 1}
    starving = {"default": [cand("a1", 50)], "hermes": [cand("b1", 0, now - share.STARVE_SECONDS - 1)]}
    assert share.plan(starving, 1, now=now) == {"hermes": 1}
    assert list(share.plan({**starving, "x": [cand("c", 99)]}, 2, now=now)) == ["hermes", "x"]
