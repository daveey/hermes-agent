"""Cards must never park forever without showing as waiting on someone. [daveey]

Regression tests for the 2026-10-21 incident: a PR-acceptance refusal stamped
"check gh authentication" into ``last_failure_error`` and the respawn guard held
the card in ``ready`` as ``blocker_auth`` on every tick, forever; scheduled cards
sat with no wake; block-loop escalations in ``triage`` and their children were
invisible as "waiting on the user"; a Desktop relaunched from a worker inherited
the worker's kanban env.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from tests.hermes_cli.kanban_guard_helpers import worker_ran

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_pr_state as _prs_mod


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
    kb.init_db()
    return home


def _continued_with_error(conn, tid, error):
    """A run that ended ``continued`` (worker checkpointed) with ``error`` stamped."""
    kb.claim_task(conn, tid)
    run_id = kb.get_task(conn, tid).current_run_id
    conn.execute("UPDATE task_runs SET outcome='continued', status='continued', ended_at=? WHERE id=?",
                 (int(time.time()) - 5, run_id))
    conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
                 "claim_expires=NULL, worker_pid=NULL, last_failure_error=? WHERE id=?", (error, tid))
    conn.commit()


# ---------------------------------------------------------------------------
# 1. Respawn guard
# ---------------------------------------------------------------------------

GATE_TEXT_OLD = ("PR acceptance infra: GitHub acceptance evidence unavailable or incomplete; check gh "
                 "authentication/API access and retry. Fix required failures, rerun infrastructure checks "
                 "or wait, then retry completion.")


@pytest.mark.parametrize("classification", ["infra", "auth", "closed_unmerged", "failure"])
def test_completion_gate_refusal_is_never_blocker_auth(kanban_home, classification):
    text = GATE_TEXT_OLD.replace("PR acceptance infra", f"PR acceptance {classification}")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="gate refused", assignee="a")
        _continued_with_error(conn, tid, text)
        assert kbd.check_respawn_guard(conn, tid) is None


def test_provider_auth_failure_still_guards(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="provider auth", assignee="a")
        _continued_with_error(conn, tid, "provider authentication failed: invalid api key")
        assert kbd.check_respawn_guard(conn, tid) == "blocker_auth"


def test_guard_hold_is_capped_and_auto_blocks_with_reason(kanban_home, monkeypatch, all_assignees_spawnable):
    """A capped guard hold reaches agent triage with its reason, not a human ask."""
    monkeypatch.setenv("HERMES_KANBAN_GUARD_HOLD_CAP_SECONDS", "600")
    spawned = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="held", assignee="a")
        _continued_with_error(conn, tid, "provider authentication failed")
        now = int(time.time())
        monkeypatch.setattr(kb.time, "time", lambda: now)
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append(a) or 1)
        assert (tid, "blocker_auth") in res.respawn_guarded
        assert kb.get_task(conn, tid).status == "ready"
        # Still inside the cap: held, not blocked.
        monkeypatch.setattr(kb.time, "time", lambda: now + 300)
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append(a) or 1)
        assert kb.get_task(conn, tid).status == "ready"
        # Past the cap: blocked with a reason naming the guard and the error.
        monkeypatch.setattr(kb.time, "time", lambda: now + 700)
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append(a) or 1)
        task = kb.get_task(conn, tid)
        assert (task.status, task.block_kind) == ("triage", "transient")
        assert tid in res.auto_blocked
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='supervisor_escalated' "
                          "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        reason = json.loads(ev["payload"])["reason"]
        assert "blocker_auth" in reason and "provider authentication failed" in reason
        assert not spawned


def test_guard_hold_clock_restarts_after_a_real_run(kanban_home, monkeypatch, all_assignees_spawnable):
    """Only an unbroken hold counts: guard events before a later claim don't add up."""
    monkeypatch.setenv("HERMES_KANBAN_GUARD_HOLD_CAP_SECONDS", "600")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="held then ran", assignee="a")
        _continued_with_error(conn, tid, "provider authentication failed")
        now = int(time.time())
        monkeypatch.setattr(kb.time, "time", lambda: now - 5000)
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
        monkeypatch.setattr(kb.time, "time", lambda: now - 100)
        _continued_with_error(conn, tid, "provider authentication failed")  # a run happened since
        monkeypatch.setattr(kb.time, "time", lambda: now)
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
        assert kb.get_task(conn, tid).status == "ready"


# ---------------------------------------------------------------------------
# 2. PR acceptance classification + rebind
# ---------------------------------------------------------------------------

def _fake_api(pr_states):
    """``_api`` double: ``pr_states`` maps PR number -> (state, merged)."""
    def api(endpoint, *, query=None, paginate=False, profile_home=None):
        if endpoint == "graphql":
            import re
            number = int(re.search(r"pullRequest\(number:(\d+)\)", query)[1])
            state = pr_states[number]
            return {"data": {"repository": {"pullRequest": {
                "headRefOid": "a" * 40, "baseRefName": "main", "state": state,
                "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
                    {"context": "ci", "app": {"databaseId": 1}}]}}}}}}
        if "/rules/branches/" in endpoint:
            return [[]]
        if "/check-runs" in endpoint:
            raise KeyError("total_count")  # a malformed page: generic failure
        raise AssertionError(endpoint)
    return api


def test_closed_unmerged_pr_gets_its_own_classification(monkeypatch):
    from hermes_cli import kanban_pr_acceptance as kpa
    monkeypatch.setattr(kpa, "_api", _fake_api({610: "CLOSED"}))
    receipt = kpa.collect_acceptance("https://github.com/acme/repo/pull/610", None)
    assert receipt["ok"] and receipt["classification"] == "closed"
    assert receipt["pr_state"] == "CLOSED"
    assert "not delivery" in receipt["detail"]


def test_catch_all_failure_names_the_phase_not_gh_auth(monkeypatch):
    from hermes_cli import kanban_pr_acceptance as kpa
    monkeypatch.setattr(kpa, "_api", _fake_api({7: "OPEN"}))
    receipt = kpa.collect_acceptance("https://github.com/acme/repo/pull/7", None)
    assert receipt["classification"] == "infra"
    assert "authentication" not in receipt["detail"]
    assert "check runs" in receipt["detail"]
    assert "KeyError" in receipt["detail"]


def test_rebind_allowed_when_bound_pr_closed_unmerged(kanban_home, monkeypatch):
    from hermes_cli import kanban_pr_acceptance as kpa
    from hermes_cli import kanban_pr_acceptance_store as store
    monkeypatch.setattr(kpa, "_api", _fake_api({610: "CLOSED", 622: "MERGED"}))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="pr", assignee=None,
                             completion_contract="https://github.com/acme/repo/pull/610")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        store.prepare_acceptance(conn, tid, run_id, {"published_pr": "https://github.com/acme/repo/pull/622"})
        row = conn.execute("SELECT completion_contract FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["completion_contract"] == "https://github.com/acme/repo/pull/622"
        assert conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='pr_rebound'",
                            (tid,)).fetchone() is not None


def test_rebind_refused_while_bound_pr_open(kanban_home, monkeypatch):
    from hermes_cli import kanban_pr_acceptance as kpa
    from hermes_cli import kanban_pr_acceptance_store as store
    monkeypatch.setattr(kpa, "_api", _fake_api({610: "OPEN", 622: "MERGED"}))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="pr", assignee=None,
                             completion_contract="https://github.com/acme/repo/pull/610")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        store.prepare_acceptance(conn, tid, run_id, {"published_pr": "https://github.com/acme/repo/pull/622"})
        row = conn.execute("SELECT completion_contract FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["completion_contract"] == "https://github.com/acme/repo/pull/610"


# ---------------------------------------------------------------------------
# 3. Scheduled invariant
# ---------------------------------------------------------------------------

def _parking_payload(conn, tid):
    ev = conn.execute("SELECT kind, payload FROM task_events WHERE task_id=? AND kind IN "
                      "('scheduled','continued') ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    return json.loads(ev["payload"] or "{}")


def test_schedule_without_marker_still_gets_a_wake(kanban_home):
    from hermes_cli import kanban_timed_park as ktp
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="sched", assignee="a")
        before = int(time.time())
        assert kb.schedule_task(conn, tid, reason=None, clock_only=True)
        wake = _parking_payload(conn, tid).get("wake_at")
        assert isinstance(wake, int) and wake >= before + ktp.DEFAULT_WAKE_SECONDS - 5


def test_human_wait_cannot_park_into_scheduled(kanban_home):
    """A card whose outstanding ask is a human answer stays blocked, not scheduled."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="needs david", assignee="a")
        kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, reason="David: unlock the iPhone", kind="needs_input")
        assert kb.get_task(conn, tid).status == "blocked"
        assert not kb.schedule_task(conn, tid, reason=None)
        assert kb.get_task(conn, tid).status == "blocked"


def _legacy_park(conn, tid, *, at, reason=None, block_kind=None):
    """A scheduled card parked by old code: ``scheduled`` event with no ``wake_at``."""
    conn.execute("UPDATE tasks SET status='scheduled', block_kind=? WHERE id=?", (block_kind, tid))
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'scheduled', ?, ?)",
                 (tid, json.dumps({"reason": reason}), at))
    conn.commit()


def test_dispatcher_wakes_legacy_scheduled_card_without_wake(kanban_home):
    from hermes_cli import kanban_continuation as kc
    now = int(time.time())
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="legacy", assignee="a")
        _legacy_park(conn, tid, at=now - 30 * 3600, reason="waiting on a cron")
        fresh = kb.create_task(conn, title="fresh legacy", assignee="a")
        _legacy_park(conn, fresh, at=now - 60, reason="waiting on a cron")
        woken = kc.wake_due_continuations(conn, now=now)
        assert tid in woken and fresh not in woken
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.get_task(conn, fresh).status == "scheduled"


def test_dispatcher_honours_marker_in_legacy_reason(kanban_home):
    from hermes_cli import kanban_continuation as kc
    now = int(time.time())
    past = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 3600))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="legacy marker", assignee="a")
        _legacy_park(conn, tid, at=now - 2 * 3600, reason=f"SCHEDULED_UNTIL={past} soak")
        assert tid in kc.wake_due_continuations(conn, now=now)


def test_human_kind_scheduled_without_wake_becomes_blocked(kanban_home):
    from hermes_cli import kanban_continuation as kc
    now = int(time.time())
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="needs input parked", assignee="a")
        _legacy_park(conn, tid, at=now - 60, reason=None, block_kind="needs_input")
        kc.wake_due_continuations(conn, now=now)
        assert kb.get_task(conn, tid).status == "blocked"


def test_diagnostic_flags_scheduled_card_without_wake(kanban_home):
    from hermes_cli import kanban_diagnostics as kd
    now = int(time.time())
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="no wake", assignee="a")
        _legacy_park(conn, tid, at=now - 60, reason=None)
        task = kb.get_task(conn, tid)
        diags = kd.compute_task_diagnostics(task, kb.list_events(conn, tid), kb.list_runs(conn, tid), now=now)
        assert "scheduled_without_wake" in [d.kind for d in diags]


# ---------------------------------------------------------------------------
# 4. Block-loop escalations count as waiting on the user
# ---------------------------------------------------------------------------

def _escalate(conn, tid):
    for _ in range(kb.BLOCK_RECURRENCE_LIMIT + 1):
        task = kb.get_task(conn, tid)
        if task.status == "triage":
            return
        if task.status == "blocked":
            kb.unblock_task(conn, tid)
        kb.claim_task(conn, tid)
        kb.block_task(conn, tid, reason="David: tap Reply timing", kind="needs_input", human_request={
            "action": "Tap Reply timing on the test device.",
            "why_human": "The timing measurement requires a physical tap by the device owner.",
            "interaction": "physical",
            "scope": "Test device reply timing measurement",
            "evidence": "The timing test is paused at the Reply timing prompt awaiting a tap.",
        })
    assert kb.get_task(conn, tid).status == "triage"


def test_block_loop_escalation_counts_as_waiting_on_user(kanban_home):
    from hermes_cli import kanban_diagnostics as kd
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="loop", assignee="a")
        _escalate(conn, tid)
        child = kb.create_task(conn, title="child", assignee="a", parents=[tid])
        assert kb.get_task(conn, child).status == "todo"

        stats = kb.board_stats(conn)
        assert stats["waiting_on_user"] >= 1
        assert tid in stats["waiting_on_user_ids"]

        diags = kd.compute_task_diagnostics(kb.get_task(conn, tid), kb.list_events(conn, tid),
                                            kb.list_runs(conn, tid), graph=kb.task_graph_context(conn, tid))
        assert "escalated_waiting_on_user" in [d.kind for d in diags]
        cdiags = kd.compute_task_diagnostics(kb.get_task(conn, child), kb.list_events(conn, child),
                                             kb.list_runs(conn, child), graph=kb.task_graph_context(conn, child))
        assert "waiting_behind_user" in [d.kind for d in cdiags]


def test_plain_triage_card_is_not_waiting_on_user(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="spec me", assignee="a", triage=True)
        assert kb.get_task(conn, tid).status == "triage"
        assert tid not in kb.board_stats(conn)["waiting_on_user_ids"]


# ---------------------------------------------------------------------------
# 5. Top-level launches strip a leaked worker env
# ---------------------------------------------------------------------------

LEAK = {
    "HERMES_DELEGATED_CHILD_CONTEXT": "/Users/x/.hermes",
    "HERMES_KANBAN_WORKSPACE": "/Users/x/.hermes/kanban/workspaces/t_45d5a361",
    "HERMES_KANBAN_DB": "/Users/x/.hermes/kanban.db",
    "HERMES_KANBAN_BOARD": "default",
    "HERMES_KANBAN_WORKSPACES_ROOT": "/Users/x/.hermes/kanban/workspaces",
}


def test_scrub_removes_leaked_worker_env():
    from hermes_cli.top_level_env import scrub_leaked_worker_env
    env = {"PATH": "/bin", **LEAK, "HERMES_KANBAN_TASK": "t_1"}
    removed = scrub_leaked_worker_env(env)
    assert env == {"PATH": "/bin"}
    assert set(removed) == set(LEAK) | {"HERMES_KANBAN_TASK"}


def test_scrub_keeps_deliberate_board_pin_without_worker_marker():
    from hermes_cli.top_level_env import scrub_leaked_worker_env
    env = {"HERMES_KANBAN_DB": "/tmp/test.db", "HERMES_KANBAN_BOARD": "x"}
    assert scrub_leaked_worker_env(env) == []
    assert env == {"HERMES_KANBAN_DB": "/tmp/test.db", "HERMES_KANBAN_BOARD": "x"}


@pytest.mark.parametrize("argv, scrubbed", [
    (["serve"], True), (["dashboard"], True), (["desktop"], True), (["gateway", "run"], True),
    (["chat"], False), (["kanban", "list"], False),
])
def test_main_scrubs_for_top_level_surfaces(monkeypatch, argv, scrubbed):
    from hermes_cli import top_level_env
    for k, v in LEAK.items():
        monkeypatch.setenv(k, v)
    top_level_env.scrub_for_top_level_launch(argv)
    import os
    assert ("HERMES_DELEGATED_CHILD_CONTEXT" not in os.environ) is scrubbed


@pytest.mark.platforms("macos")
def test_desktop_relaunch_after_update_gets_scrubbed_env(monkeypatch, tmp_path):
    """``hermes update`` run from a worker reopens Desktop with ``open``; the app must not
    inherit the worker's kanban/delegation env (Hermes.app pid 59331, 2026-10-21)."""
    from hermes_cli import main_desktop as md
    for k, v in LEAK.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_45d5a361")
    exe = tmp_path / "Hermes.app" / "Contents" / "MacOS" / "Hermes"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(md, "_desktop_packaged_executable", lambda d: exe)
    monkeypatch.setattr(md, "_desktop_relaunch_after_rebuild_enabled", lambda: True)
    monkeypatch.setattr(md.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(md.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)))
    md._relaunch_stopped_desktops(tmp_path, [[]])
    assert calls, "desktop was not reopened"
    env = calls[0][1].get("env")
    assert env is not None
    assert not ({"HERMES_KANBAN_TASK", *LEAK} & set(env))


def test_guard_hold_posts_one_comment_and_no_inbox_card_naming_pr_and_checks(
        kanban_home, monkeypatch, all_assignees_spawnable):
    """Within 30 min of a hold the card gets ONE kanban comment + ONE fix card (assignee default)
    naming the PR and its failing checks, NO Inbox card (Inbox = owner-only decisions, AGENTS.md),
    one ``respawn_guarded`` event per hold instead of one per tick, and the notice does not
    restart the hold clock (the 6 h cap still fires). [daveey]"""
    pr = "https://github.com/daveey/hermes/pull/19"
    posted, cards = [], []
    monkeypatch.setattr(kbd._guard_notice, "_pr_failing_checks", lambda url: "2 failing check(s): lint, unit")
    real_run = kbd._guard_notice.subprocess.run

    def _spy_run(cmd, *a, **k):
        if "inbox" in cmd:
            posted.append(cmd)
        return real_run(["true"], *a, **k)
    monkeypatch.setattr(kbd._guard_notice.subprocess, "run", _spy_run)
    monkeypatch.setattr(kbd._guard_notice, "_file_fix_card", lambda conn, tid, reason, body: cards.append((tid, reason, body)))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="pr held", assignee="a")
        kb.add_comment(conn, tid, "a", f"opened {pr}")
        now = int(time.time())
        monkeypatch.setattr(kb.time, "time", lambda: now)
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
        assert (tid, "active_pr") in res.respawn_guarded
        monkeypatch.setattr(kb.time, "time", lambda: now + 600)
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
        monkeypatch.setattr(kb.time, "time", lambda: now + 1900)
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)  # second tick: no duplicate
        assert not posted and not hasattr(kbd._guard_notice, "_post_inbox_card")
        assert [(c[0], c[1]) for c in cards] == [(tid, "active_pr")] and pr in cards[0][2]
        notes = [c for c in kb.list_comments(conn, tid) if c.body.startswith("[respawn-guard]")]
        assert len(notes) == 1
        assert pr in notes[0].body and "lint, unit" in notes[0].body and "active_pr" in notes[0].body
        guarded = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'respawn_guarded'",
                               (tid,)).fetchone()[0]
        assert guarded == 1  # 4 ticks, one unbroken hold -> one event
        # The notice must not restart the hold clock: the 6 h cap still blocks.
        monkeypatch.setattr(kb.time, "time", lambda: now + 6 * 3600 + 100)
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
        assert kb.get_task(conn, tid).status == "triage"


def test_guard_event_relogged_after_hold_is_broken(kanban_home, monkeypatch, all_assignees_spawnable):
    """A human comment breaks the hold: the next held tick logs a fresh ``respawn_guarded`` event. [daveey]"""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="pr held", assignee="a")
        kb.add_comment(conn, tid, "a", "opened https://github.com/daveey/hermes/pull/19")
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
        kb.add_comment(conn, tid, "david", "still waiting on review")
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
        n = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'respawn_guarded'",
                         (tid,)).fetchone()[0]
        assert n == 2


class _Proc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _gh_states(monkeypatch, states):
    """Route the real ``_gh_pr_state`` through a fake ``gh``: url -> state, or an Exception/int rc."""
    from hermes_cli import kanban_db_dispatch_pr_state as prs
    monkeypatch.setattr(prs, "_gh_pr_state", _REAL_GH_PR_STATE)
    prs.clear_cache()
    calls = []

    def _run(cmd, *a, **k):
        calls.append(cmd)
        st = states[cmd[cmd.index("view") + 1]]
        if isinstance(st, BaseException):
            raise st
        if isinstance(st, int):
            return _Proc(st, stderr="gh: could not resolve host")
        return _Proc(0, json.dumps({"state": st}))
    monkeypatch.setattr(prs.subprocess, "run", _run)
    return calls


_REAL_GH_PR_STATE = _prs_mod._gh_pr_state
PR_A = "https://github.com/Metta-AI/coworld-qwop/pull/1"
PR_B = "https://github.com/daveey/hermes/pull/19"


@pytest.mark.parametrize("states,expected", [
    ({PR_A: "MERGED"}, None),                         # (b) merged PR -> released
    ({PR_A: "CLOSED"}, None),                         # closed unmerged -> nothing to duplicate
    ({PR_A: "OPEN"}, "active_pr"),                    # (c) open PR -> still held
    ({PR_A: 1}, "active_pr"),                         # (d) gh error exit -> fail closed
    ({PR_A: OSError("gh not found")}, "active_pr"),   # (d) gh missing -> fail closed
    ({PR_A: "MERGED", PR_B: "OPEN"}, "active_pr"),    # one PR still open -> held
    ({PR_A: "MERGED", PR_B: 1}, "active_pr"),         # one PR unknown -> held
    ({PR_A: "MERGED", PR_B: "CLOSED"}, None),         # every PR finished -> released
])
def test_active_pr_guard_checks_pr_state(kanban_home, monkeypatch, states, expected):
    calls = _gh_states(monkeypatch, states)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="pr card", assignee="a")
        kb.add_comment(conn, tid, "a", "opened " + " and ".join(states))
        assert kbd.check_respawn_guard(conn, tid) == expected
        assert {c[c.index("view") + 1] for c in calls} <= set(states)


def test_pr_state_is_cached(kanban_home, monkeypatch):
    """Merged is cached for good, so a held tick does not shell out to gh per card per tick."""
    calls = _gh_states(monkeypatch, {PR_A: "MERGED"})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="pr card", assignee="a")
        kb.add_comment(conn, tid, "a", f"opened {PR_A}")
        for _ in range(3):
            assert kbd.check_respawn_guard(conn, tid) is None
    assert len(calls) == 1


def test_merged_pr_card_gets_a_worker(kanban_home, monkeypatch, all_assignees_spawnable):
    """End to end: a ready card whose only PR merged is spawned, not held (t_d507533c). [daveey]"""
    _gh_states(monkeypatch, {PR_A: "MERGED"})
    spawned = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="pr merged", assignee="a")
        kb.add_comment(conn, tid, "a", f"opened {PR_A}")
        res = kbd.dispatch_once(conn, spawn_fn=lambda task, *a, **k: spawned.append(task.id) or 1)
        assert tid not in dict(res.respawn_guarded)
        assert spawned == [tid]


def test_pr_state_lookup_uses_broker_prefix_and_resolved_gh(kanban_home, monkeypatch):
    """A service dispatcher has a minimal PATH and no ambient gh login; the PR-state read must
    use the configured ``kanban.pr_acceptance.gh_command_prefix`` and ~/bin gh, or every lookup
    fails closed and a merged PR holds its card for the whole hold cap. [daveey]"""
    import types
    from hermes_cli import kanban_db_dispatch_pr_state as prs
    from hermes_cli import kanban_pr_acceptance as kpa
    from hermes_platform.resolver import github as ghres

    monkeypatch.setattr(prs, "_gh_pr_state", _REAL_GH_PR_STATE)
    prs.clear_cache()
    prefix = ["/opt/broker", "exec", "--scope", "user/github", "--"]
    monkeypatch.setattr(kpa, "_gh_command_prefix", lambda home: list(prefix))
    monkeypatch.setattr(ghres, "resolve_gh",
                        lambda path=None: types.SimpleNamespace(found=True, command=["/home/x/bin/gh"]))
    calls = []
    monkeypatch.setattr(prs.subprocess, "run",
                        lambda cmd, *a, **k: calls.append(cmd) or _Proc(0, json.dumps({"state": "MERGED"})))
    assert prs.pr_state(PR_A) == "MERGED"
    assert calls == [[*prefix, "/home/x/bin/gh", "pr", "view", PR_A, "--json", "state,comments,headRefOid,baseRefName"]]

    # Broken broker config: fall back to bare gh (still fail-closed on error), never raise.
    prs.clear_cache()
    calls.clear()
    def _boom(home):
        raise kpa._GateSetupError("bad config")
    monkeypatch.setattr(kpa, "_gh_command_prefix", _boom)
    monkeypatch.setattr(prs.subprocess, "run", lambda cmd, *a, **k: calls.append(cmd) or _Proc(1))
    assert prs.pr_state(PR_A) is None
    assert calls[0][:4] == ["gh", "pr", "view", PR_A]


@pytest.fixture(autouse=True)
def _worker_ran(worker_ran):
    """These guard tests model cards whose worker already ran."""
