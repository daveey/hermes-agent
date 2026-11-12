"""A ready card the dispatcher passes over carries ONE durable skip reason per ready episode (kanban-dd62aef001).

Hub 2026-11-10: t_01a1c7a0c8139180 (pinned claude-opus-5-5/anthropic) sat ready 33 min with free slots and no
``claimed``/``skipped_*``/``respawn_guarded`` event; the chat-wait notice said "1 card(s) ahead of it, 4/7 worker
slots busy" and advised a priority bump, which did nothing.
"""
import pytest

from hermes_cli import kanban_chat_wait as cw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_guard_notice as guard_notice
from hermes_cli import kanban_dispatch_skip as skips
from hermes_cli import kanban_parent_unclaimed as pu
from hermes_cli import kanban_provider_walls as walls
from hermes_cli import kanban_queue_position as qp
from hermes_cli import kanban_worker_node as kwn
from tests.hermes_cli.test_kanban_quota_episode import board as board, routing  # noqa: PLC0414 -- the self-alias is load-bearing: it suppresses F811 for the pytest fixture parameter shadowing this import

START = 1_800_000_000  # 2027-01-15T08:00:00Z
RESET = START + 2 * 3600  # 10:00Z the same day
PIN = ("claude-opus-5-5", "anthropic")
PRIORITY_WORDS = ("bump", "re-place on a capable gateway")


def _clock(monkeypatch):
    clock = [START]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])
    return clock


def _skips(conn, tid):
    return [kb._json_dict(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (tid, skips.EVENT))]


def _tick(conn, **kw):
    seen = []
    dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: seen.append(task.id), **kw)
    return seen


def _walled(monkeypatch, until=RESET):
    calls = []

    def wall(provider, model):
        calls.append((provider, model))
        return until if (model, provider) == PIN else None
    monkeypatch.setattr(walls, "live_quota_wall", wall)
    return calls


def _notice(conn, tid):
    row = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                       (tid, cw.NOTICE_KIND)).fetchone()
    return kb._json_dict(row[0]) if row else None


def test_fresh_pin_on_quota_wall_is_held_with_one_reason_and_the_notice_names_it(
        board, monkeypatch, all_assignees_spawnable):
    clock = _clock(monkeypatch)
    routing(monkeypatch)
    calls = _walled(monkeypatch)
    monkeypatch.setattr(qp, "_running_and_limit", lambda *_a: (4, 7))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="bb-tuner pinned", assignee="a", model_override=PIN[0],
                             provider_override=PIN[1], session_id="chat")
        free = kb.create_task(conn, title="unpinned sibling", assignee="a")
        spawned = []
        for _ in range(4):
            spawned += _tick(conn)
            clock[0] += 60
        assert tid not in spawned and free in spawned  # free slots: only the walled pin is held
        assert ("anthropic", "claude-opus-5-5") in calls
        assert _skips(conn, tid) == [{"reason": "pinned_quota_wall", "detail": "claude-opus-5-5 (anthropic)",
                                      "model": PIN[0], "provider": PIN[1], "until": RESET}]
        task = kb.get_task(conn, tid)
        assert task.status == "ready" and not task.claim_lock
        place = qp.position(conn, task)
        assert place["lane"] == "local" and place["skip"]["reason"] == "pinned_quota_wall"
        line = qp.describe(place)
        assert "held: pinned model claude-opus-5-5 (anthropic) quota-walled until 10:00Z" in line
        assert not any(w in line for w in PRIORITY_WORDS)

        clock[0] = START + 31 * 60
        assert cw.sweep(conn, now=clock[0]) == 1
        payload = _notice(conn, tid)
        assert payload["dispatch_skip"] == {"reason": "pinned_quota_wall", "detail": "claude-opus-5-5 (anthropic)",
                                            "until": RESET}
        for text in (cw.notice_text(payload), pu.notice_text(payload), pu.ready_hint(conn, task, now=clock[0])):
            assert "held: pinned model claude-opus-5-5 (anthropic) quota-walled until 10:00Z" in text
            assert not any(w in text for w in PRIORITY_WORDS), text
            assert skips.UNRECORDED not in text


def test_walled_pin_dry_run_holds_without_writing(board, monkeypatch, all_assignees_spawnable):
    _clock(monkeypatch)
    routing(monkeypatch)
    _walled(monkeypatch)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="pinned", assignee="a", model_override=PIN[0], provider_override=PIN[1])
        res = dispatch.dispatch_once(conn, spawn_fn=lambda *_a: None, dry_run=True)
        assert res.pinned_quota_held == [(tid, PIN[0], PIN[1], float(RESET))]
        assert res.clock_held[tid] == ("pinned_quota_wall", RESET)
        assert res.spawned == [] and _skips(conn, tid) == []


@pytest.mark.parametrize("case", ["wall_cleared", "wall_past", "unpinned", "has_run"])
def test_pin_hold_only_for_a_fresh_pin_on_a_live_wall(board, monkeypatch, all_assignees_spawnable, case):
    clock = _clock(monkeypatch)
    routing(monkeypatch)
    _walled(monkeypatch, until={"wall_cleared": None, "wall_past": START - 1}.get(case, RESET))
    with kbc.connect() as conn:
        pin = {} if case == "unpinned" else {"model_override": PIN[0], "provider_override": PIN[1]}
        tid = kb.create_task(conn, title="pinned", assignee="a", **pin)
        if case == "has_run":
            assert kb.claim_task(conn, tid, claimer=kb._claimer_id()) is not None  # opens a run
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, current_run_id=NULL WHERE id=?",
                             (tid,))
            clock[0] += 1
        assert skips.pinned_quota_hold(conn, tid) is None


def test_per_profile_capped_row_gets_one_event(board, monkeypatch, all_assignees_spawnable):
    clock = _clock(monkeypatch)
    routing(monkeypatch)
    _walled(monkeypatch, until=None)
    with kbc.connect() as conn:
        first = kb.create_task(conn, title="first", assignee="a", priority=5)
        second = kb.create_task(conn, title="second", assignee="a")
        for _ in range(3):
            _tick(conn, max_in_progress_per_profile=1)
            clock[0] += 60
        assert kb.get_task(conn, first).status == "running"
        assert _skips(conn, second) == [{"reason": "per_profile_capped", "detail": "a", "running": 1}]
        place = qp.position(conn, kb.get_task(conn, second))
        assert "skipped: assignee a is at its per-profile worker cap" in qp.describe(place)


def test_ineligible_row_gets_one_event_per_reason(board, monkeypatch, all_assignees_spawnable):
    clock = _clock(monkeypatch)
    routing(monkeypatch)
    _walled(monkeypatch, until=None)
    refusal = {"code": "labels_mismatch"}
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="elsewhere", assignee="a")
        monkeypatch.setattr(kwn, "dispatch_refusals",
                            lambda _c, status: {tid: refusal["code"]} if status == "ready" else {})
        for _ in range(3):
            assert _tick(conn) == []
            clock[0] += 60
        refusal["code"] = "pinned_only"
        _tick(conn)
        _tick(conn)
        assert [(p["reason"], p["detail"]) for p in _skips(conn, tid)] == [
            ("skipped_ineligible", "labels_mismatch"), ("skipped_ineligible", "pinned_only")]
        assert skips.latest(conn, tid)["detail"] == "pinned_only"


def test_unknown_reason_ready_30_min_with_free_slots_says_unrecorded(board, monkeypatch):
    clock = _clock(monkeypatch)
    monkeypatch.setattr(qp, "_running_and_limit", lambda *_a: (4, 7))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="mystery wait", assignee="a", session_id="chat")
        clock[0] = START + 31 * 60
        assert cw.sweep(conn, now=clock[0]) == 1
        payload = _notice(conn, tid)
        assert payload["skip_unrecorded"] is True and "dispatch_skip" not in payload
        for text in (cw.notice_text(payload), pu.notice_text(payload),
                     pu.ready_hint(conn, kb.get_task(conn, tid), now=clock[0])):
            assert skips.UNRECORDED in text, text
            assert not any(w in text for w in PRIORITY_WORDS), text


@pytest.mark.parametrize("running, minutes", [(7, 31), (4, 29)])
def test_full_host_or_short_wait_keeps_the_capacity_advice(board, monkeypatch, running, minutes):
    clock = _clock(monkeypatch)
    monkeypatch.setattr(qp, "_running_and_limit", lambda *_a: (running, 7))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="queued", assignee="a", session_id="chat")
        place = qp.position(conn, kb.get_task(conn, tid))
        assert skips.notice_fields(place, minutes * 60) == {}
        clock[0] = START + minutes * 60
        hint = pu.ready_hint(conn, kb.get_task(conn, tid), now=clock[0])
        assert hint is None or (skips.UNRECORDED not in hint and "re-place on a capable gateway" in hint)


def test_ordering_skip_keeps_the_capacity_advice(board, monkeypatch):
    _clock(monkeypatch)
    monkeypatch.setattr(qp, "_running_and_limit", lambda *_a: (4, 7))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="deferred", assignee="a")
        assert skips.record(conn, tid, "claim_deferred", detail="capacity")
        place = qp.position(conn, kb.get_task(conn, tid))
        payload = skips.notice_fields(place, 3600)
        assert skips.is_ordering(payload["dispatch_skip"])
        assert skips.notice_advice(payload) is None
        assert "relative priority bump" in qp.describe(place)


def test_per_profile_cap_is_an_ordering_skip(board, monkeypatch):
    _clock(monkeypatch)
    monkeypatch.setattr(qp, "_running_and_limit", lambda *_a: (4, 7))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="capped", assignee="a")
        assert skips.record(conn, tid, "per_profile_capped", detail="a")
        payload = skips.notice_fields(qp.position(conn, kb.get_task(conn, tid)), 3600)
        assert skips.is_ordering(payload["dispatch_skip"]) and skips.notice_advice(payload) is None
        assert not skips.is_ordering({"reason": "claim_deferred", "detail": "labels"})


def test_a_moved_quota_reset_appends_other_reasons_ignore_until(board, monkeypatch):
    _clock(monkeypatch)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="walled", assignee="a")
        detail = "claude-opus-5-5 (anthropic)"
        assert skips.record(conn, tid, skips.PINNED_QUOTA_WALL, detail=detail, until=RESET)
        assert not skips.record(conn, tid, skips.PINNED_QUOTA_WALL, detail=detail, until=RESET + 0.4)
        assert skips.record(conn, tid, skips.PINNED_QUOTA_WALL, detail=detail, until=RESET + 3600)
        assert skips.latest(conn, tid)["until"] == RESET + 3600  # the notice names the new reset
        assert skips.record(conn, tid, "owner_down", detail="x", until=RESET)
        assert not skips.record(conn, tid, "owner_down", detail="x", until=RESET + 60)
        assert [p.get("until") for p in _skips(conn, tid)] == [RESET, RESET + 3600, RESET]


def test_review_lane_is_never_pin_held(board, monkeypatch):
    _clock(monkeypatch)
    _walled(monkeypatch)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="pinned", assignee="a", model_override=PIN[0], provider_override=PIN[1])
        res = dispatch.DispatchResult()
        assert not skips.hold_pinned_quota(conn, tid, res, lane="review")
        assert skips.hold_pinned_quota(conn, tid, res, lane="ready") and res.pinned_quota_held


def test_skip_records_do_not_hide_other_latest_event_readers(board, monkeypatch):
    from hermes_cli import kanban_capsule_admission as capsule
    from hermes_cli.kanban_db_dispatch_preclaim import note_nonspawnable
    _clock(monkeypatch)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="noisy", assignee="ghost")
        other = kb.create_task(conn, title="capsule", assignee="ghost")
        for i in range(2):
            note_nonspawnable(conn, tid, "ghost", dry_run=False)
            skips.record(conn, tid, "claim_deferred", detail=f"d{i}")
            capsule._record_refusal(conn, other, {"code": "capsule_runs_on"})
            skips.record(conn, other, "capsule_refused", detail=f"c{i}")

        def kinds(task_id):
            return [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id = ?", (task_id,))]
        assert kinds(tid).count("skipped_nonspawnable") == 1
        assert kinds(other).count(capsule.REFUSAL_EVENT) == 1
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "changes_requested", {})
        skips.record(conn, tid, "per_profile_capped", detail="ghost")
        assert kb.goal_run_status(conn, tid) == "changes_requested"
        from hermes_cli.kanban_diagnostics import _ready_hold_reason
        with kb.write_txn(conn):
            kb._append_event(conn, other, "respawn_guarded", {"reason": "active_pr"})
        skips.record(conn, other, "per_profile_capped", detail="ghost")
        assert _ready_hold_reason(kb.get_task(conn, other), kb.list_events(conn, other), {}) == "respawn_guarded"


def test_skip_records_neither_reset_a_guard_hold_nor_grow_without_bound(board, monkeypatch):
    clock = _clock(monkeypatch)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="guarded", assignee="a")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "respawn_guarded", {"reason": "active_pr"})
        started = guard_notice.guard_hold_started_at(conn, tid)
        clock[0] += 10
        assert skips.record(conn, tid, "per_profile_capped", detail="a")
        assert guard_notice.guard_hold_reason(conn, tid) == "active_pr"
        assert guard_notice.guard_hold_started_at(conn, tid) == started
        assert qp.position(conn, kb.get_task(conn, tid))["skip"]["reason"] == "per_profile_capped"
        for i in range(skips.MAX_PER_EPISODE + 5):
            skips.record(conn, tid, "claim_deferred", detail=f"code{i}")
        assert len(_skips(conn, tid)) == skips.MAX_PER_EPISODE
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "claimed", {})
        assert skips.latest(conn, tid) is None  # a claim ends the ready episode
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
        assert not skips.record(conn, tid, "per_profile_capped", detail="a")  # off the lane: nothing waits
