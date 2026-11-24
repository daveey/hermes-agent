"""External-run cards: the rules review round 1 of PR #3745 found missing. [daveey]

Each test is red on the reviewed head 1de163d286: remote shell probes and the 2-interval rule, staleness of a
failing record, the auth blocker, per-ref / quota classification, the PR verdict, real cadence with jitter, the
deadline wake of an external park, a two-database sync and the timeline through the dispatcher's wake tick.
"""
from __future__ import annotations

import itertools
import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_ci_progress as kcp
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_external_state as ks
from hermes_cli import kanban_external_watch as kew
from hermes_cli import kanban_shared_blockers as ksb
from hermes_cli import kanban_wake_check as kwc
from hermes_cli.kanban_external_adapters import github as gha
from tests.hermes_cli import test_kanban_external_watch as helpers

EVERY, REPO, T0 = helpers.EVERY, helpers.REPO, helpers.T0
events, park, status, tick, url = helpers.events, helpers.park, helpers.status, helpers.tick, helpers.url
kanban_home, gh, clock = helpers.kanban_home, helpers.gh, helpers.clock


@pytest.fixture
def mono(monkeypatch, clock):
    """The sweep's spacing clock follows the fake wall clock, so real cadence is what the test sees."""
    monkeypatch.setattr(kcp, "_mono_now", lambda: clock.now)
    return clock


def sweep(conn, gh):
    return kcp.sweep(conn, fetch=gh.fetch, fetch_run=gh.fetch_run, fetch_runners=lambda repo: [])


def _edit_park(conn, tid, fn):
    with kb.write_txn(conn):
        row = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='scheduled' "
                           "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        payload = json.loads(row[1] or "{}")
        fn(payload)
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row[0]))


def _sync_as(conn, gateway):
    conn.execute("CREATE TABLE IF NOT EXISTS kanban_sync_state (gateway_id TEXT NOT NULL)")
    conn.execute("DELETE FROM kanban_sync_state")
    conn.execute("INSERT INTO kanban_sync_state (gateway_id) VALUES (?)", (gateway,))


def _shell_apply(conn, tid, rc, out, now):
    st = kwc.get_state(conn, tid)
    return kwc.apply_result(conn, tid, st["cmd"], rc, out, generation=st["generation"], now=int(now))


def _first_states(conn, tid, start, minutes, step=1):
    first = {}
    for m in range(0, minutes, step):
        st = kew.card_state(conn, tid, now=start + m * 60)
        first.setdefault(st["state"] if st else "grey", m)
    return first


# --- 1: a remote shell probe obeys the 2-interval rule -------------------------------------------------------
def test_remote_shell_probe_is_unknown_after_one_interval_and_red_after_two(kanban_home):
    """A worker-run probe (20 of 24 live parks), every=900 s: the home board reads unknown >1x, red >2x."""
    with kbc.connect_closing() as conn:
        tid = park(conn, kind="external", ref="a slurm job", wake_check="probe.sh")
        _edit_park(conn, tid, lambda p: p.update(probe_host="worker-a1"))
        now = time.time()
        with kb.write_txn(conn):
            conn.execute("DELETE FROM task_wake_checks WHERE task_id=?", (tid,))  # the probe runs on worker-a1
            kb._append_event(conn, tid, kew.WATCH_EVENT, {
                "adapter": "shell", "scope": "", "host": "worker-a1", "job": "running", "service": "ok",
                "observed_at": now, "next_due_at": now + 900, "every": 900, "consecutive_failures": 0,
                "state": "external_running"})
        first = _first_states(conn, tid, now, 60)
    assert first["external_running"] == 0
    assert 15 < first["external_unknown"] <= 19   # >1x every (15 min), within jitter + one tick of grace
    assert 30 <= first["external_down"] <= 31     # >2x every (30 min)


def test_shell_probe_on_a_synced_board_heartbeats_every_interval(kanban_home):
    """The probe host writes one record per interval (not per check faster than that), so a reader on another
    gateway has a freshness signal at ``every``."""
    with kbc.connect_closing() as conn:
        _sync_as(conn, "worker-a1")
        tid = park(conn, kind="external", ref="a slurm job", wake_check="probe.sh")
        t = int(time.time())
        for i in range(5):  # checks every 900 s
            assert _shell_apply(conn, tid, 1, "", t + i * 900) is None
        recs = events(conn, tid, kew.WATCH_EVENT)
        assert len(recs) == 5 and {r["host"] for r in recs} == {"worker-a1"}
        assert [round(r["observed_at"] - t) for r in recs] == [0, 900, 1800, 2700, 3600]


def test_shell_probe_on_an_unsynced_board_writes_on_change_only(kanban_home):
    with kbc.connect_closing() as conn:
        tid = park(conn, kind="external", ref="a slurm job", wake_check="probe.sh")
        t = int(time.time())
        for i in range(5):
            _shell_apply(conn, tid, 1, "", t + i * 900)
        assert len(events(conn, tid, kew.WATCH_EVENT)) == 1
        # t_01a20c1b37830eaf: fresh last_at does not establish job progress.
        assert kew.card_state(conn, tid, now=t + 4 * 900 + 60)["state"] == "external_unknown"


# --- 2: staleness applies to failing records too -------------------------------------------------------------
@pytest.mark.parametrize("service", ["down", "unreachable", "probe_broken", "rate_limited"])
def test_one_failed_poll_then_silence_goes_red(service):
    rec = ks.next_record(None, {"job": "unknown", "service": service, "detail": "x"}, now=T0, every=EVERY,
                         adapter="github_run", scope="o/r", rand=0.5)
    assert ks.derive(rec, now=T0 + 0.5 * EVERY, every=EVERY)["state"] == "external_unknown"
    for k in (3, 10, 1000):
        got = ks.derive(rec, now=T0 + k * EVERY, every=EVERY)
        assert (got["state"], got["health"]) == ("external_down", "host_silent"), k


def test_one_5xx_then_a_dead_host_is_red_through_the_sweep(kanban_home, gh, clock):
    with kbc.connect_closing() as conn:
        tid = park(conn)
        gh.mode = "5xx"
        tick(conn, gh, clock, 0)
        got = [kew.card_state(conn, tid, now=clock.now + k * EVERY)["state"] for k in (0.5, 3, 10)]
    assert got == ["external_unknown", "external_down", "external_down"]


def test_auth_failed_stays_auth_failed_when_stale():
    rec = ks.next_record(None, {"job": "unknown", "service": "auth_failed"}, now=T0, every=EVERY,
                         adapter="github_run", scope="o/r", rand=0.5)
    got = ks.derive(rec, now=T0 + 50 * EVERY, every=EVERY)
    assert (got["state"], got["health"]) == ("external_down", "auth_failed")


def test_a_local_sweep_that_is_behind_reads_unknown_not_host_silent():
    rec = ks.next_record(None, {"job": "running", "service": "ok"}, now=T0, every=EVERY, adapter="github_run",
                         scope="o/r", rand=0.5)
    got = ks.derive(rec, now=T0 + 3 * EVERY, every=EVERY, host_alive=True)
    assert got["state"] == "external_unknown" and "behind" in got["reason"]
    assert ks.derive(rec, now=T0 + 3 * EVERY, every=EVERY)["state"] == "external_down"
    # bounded: a card the live host never reaches is red after BEHIND_MAX intervals
    assert ks.derive(rec, now=T0 + (ks.BEHIND_MAX + 0.1) * EVERY, every=EVERY, host_alive=True)["state"] == "external_down"


def test_more_cards_than_the_sweep_budget_never_read_red(kanban_home, gh, mono, monkeypatch):
    monkeypatch.setattr(kcp, "MAX_CARDS_PER_SWEEP", 1)
    seen: dict[str, int] = {}
    with kbc.connect_closing() as conn:
        tids = [park(conn, 5000 + i) for i in range(12)]
        for _ in range(60):
            mono.set(mono.now + 60)
            sweep(conn, gh)
            for t in tids:
                st = kew.card_state(conn, t, now=mono.now)
                seen[st["state"]] = seen.get(st["state"], 0) + 1
    assert "external_down" not in seen and seen.get("external_running", 0) > 0, seen


# --- 3: auth_failed asserts the blocker and wakes one probe card ---------------------------------------------
def test_401_asserts_one_service_down_blocker_and_wakes_one_probe(kanban_home, gh, clock):
    with kbc.connect_closing() as conn:
        tids = [park(conn, 6000 + i) for i in range(3)]
        gh.mode = "401"
        woken = tick(conn, gh, clock, 0)
        bid = kew.blocker_id("github_run", REPO)
        open_ = [k for k, v in ksb.blocker_states(conn).items() if k.startswith("service-down") and v.is_open]
        assert open_ == [bid]
        assert len(woken) == 1 and status(conn, woken[0]) == "ready"
        assert events(conn, woken[0], kew.SERVICE_DOWN)[-1]["health"] == "auth_failed"
        for t in tids:
            if t == woken[0]:
                continue
            got = kew.card_state(conn, t, now=clock.now + 1)
            assert (got["state"], got["health"]) == ("external_down", "auth_failed")
            assert got["external"]["blocker_id"] == bid and got["external"]["triage_card"] == woken[0]
        assert tick(conn, gh, clock) == []  # the blocker is open: nobody else is woken


def test_no_login_on_this_host_is_red_but_wakes_nobody_and_asserts_no_blocker(kanban_home, gh, clock):
    """``gh`` has no login HERE (exit 4): the host's trouble. Red, the hourly operator warning stays the remedy."""
    with kbc.connect_closing() as conn:
        tid = park(conn)

        def no_login(repo, run_id):
            raise kcp.NoCredential("gh api exit 4: To get started with GitHub CLI, please run: gh auth login")
        for _ in range(3):
            clock.set(clock.now + EVERY)
            kcp._LAST_CHECK.clear()
            assert kcp.sweep(conn, fetch=no_login, fetch_run=gh.fetch_run, fetch_runners=lambda repo: []) == []
        got = kew.card_state(conn, tid, now=clock.now + 1)
        assert (got["state"], got["health"]) == ("external_down", "auth_failed")
        assert status(conn, tid) == "scheduled"
        assert not [k for k in ksb.blocker_states(conn) if k.startswith("service-down")]


# --- 4: classification ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("err,health", [
    ("gh api exit 1: HTTP 404: Not Found (https://api.github.com/repos/x/y/actions/runs/1/jobs)", "probe_broken"),
    ("gh api exit 1: HTTP 410: Gone", "probe_broken"),
    ("gh api exit 1: HTTP 422: Unprocessable Entity", "probe_broken"),
    ("gh api exit 1: HTTP 403: Resource not accessible by integration", "auth_failed"),
    ("gh api exit 1: HTTP 401: Bad credentials", "auth_failed"),
    ("GitHub quota guard refused/failed (Denied); inspect local attribution ledger. (exit 75)", "rate_limited"),
    ("shared GitHub quota brake is on; CI not read this time", "rate_limited"),
    ("gh api exit 1: HTTP 403: API rate limit exceeded for installation ID 1", "rate_limited"),
    ("gh api exit 1: HTTP 403: You have exceeded a secondary rate limit", "rate_limited"),
    ("gh api exit 1: HTTP 429: too many requests retry-after: 60", "rate_limited"),
    ("gh api exit 1: HTTP 502: Bad Gateway", "down"),
    ("gh api exit 1: HTTP 503: Service Unavailable", "down"),
    ("Command 'gh' timed out after 20 seconds", "unreachable"),
    ("dial tcp: lookup api.github.com: no such host", "unreachable"),
    ("Expecting value: line 1 column 1 (char 0)", "down"),  # an outage's HTML page: the service, not the ref
])
def test_classify(err, health):
    assert gha.classify(err)[0] == health


def test_one_deleted_run_never_blocks_the_repo_and_wakes_only_itself(kanban_home, gh, clock):
    with kbc.connect_closing() as conn:
        bad, good = park(conn, 7001), park(conn, 7002)
        gh.run_modes["7001"] = "404"
        woken = []
        for _ in range(3):
            woken += tick(conn, gh, clock)
            assert not [k for k in ksb.blocker_states(conn) if k.startswith("service-down")]
        assert woken == [bad]
        assert not events(conn, bad, kew.SERVICE_DOWN)
        assert len(events(conn, bad, kew.REF_BROKEN)) == 1
        assert kew.card_state(conn, good, now=clock.now + 1)["state"] == "external_running"
        assert status(conn, good) == "scheduled"


def test_a_failure_one_card_has_alone_is_not_a_scope_outage(kanban_home, gh, clock):
    """Run 7101 answers 5xx while its sibling on the same repo reads fine: no repo-wide blocker."""
    with kbc.connect_closing() as conn:
        bad, good = park(conn, 7101), park(conn, 7102)
        gh.run_modes["7101"] = "5xx"
        woken = []
        for _ in range(3):
            woken += tick(conn, gh, clock)
        assert not [k for k in ksb.blocker_states(conn) if k.startswith("service-down")]
        assert woken == [bad] and not events(conn, bad, kew.SERVICE_DOWN)
        assert events(conn, bad, kew.REF_BROKEN)[0]["health"] == "down"
        assert status(conn, good) == "scheduled"


def test_quota_guard_refusal_is_unknown_never_red(kanban_home, gh, clock):
    with kbc.connect_closing() as conn:
        tid = park(conn)
        gh.mode = "quota"
        for _ in range(6):
            assert tick(conn, gh, clock, 700) == []
            got = kew.card_state(conn, tid, now=clock.now + 1)
            assert (got["state"], got["health"]) == ("external_unknown", "rate_limited")
        assert not [k for k in ksb.blocker_states(conn) if k.startswith("service-down")]


# --- 5: the PR adapter's verdict ------------------------------------------------------------------------------
HEAD = "a" * 40
OLD = "2026-11-20T00:00:00Z"


def _api(runs, *, required=None, checks=None, calls=None):
    def api(path):
        if calls is not None:
            calls.append(path)
        if "/actions/runs?" in path:
            return {"workflow_runs": runs}
        if "/check-runs" in path:
            return {"check_runs": checks or []}
        if "/rules/branches/" in path:
            return []
        if "/branches/" in path:
            ctx = sorted(required or [])
            return {"protection": {"enabled": bool(ctx), "required_status_checks": {"contexts": ctx}}}
        if "/pulls/" in path:
            return {"state": "open", "merged": False, "mergeable": True, "mergeable_state": "clean",
                    "head": {"sha": HEAD}, "base": {"ref": "main"}, "updated_at": OLD}
        return {}
    return api


def _pr(monkeypatch, runs, **kw):
    api = _api(runs, **kw)
    monkeypatch.setattr(kcp, "_gh_api", api)
    gha._REQUIRED.clear()
    return gha.poll(f"https://github.com/{REPO}/pull/9", "", now=time.time(), api=api).poll


def _run(i, status_, conclusion=None, name="Tests", wf=None, updated=OLD):
    return {"id": i, "status": status_, "conclusion": conclusion, "name": name, "workflow_id": wf or name,
            "updated_at": updated}


def _check(i, name, status_="completed", conclusion="success"):
    return {"id": i, "name": name, "status": status_, "conclusion": conclusion}


def test_pr_superseded_cancelled_run_does_not_decide(monkeypatch):
    got = _pr(monkeypatch, [_run(1, "completed", "cancelled"), _run(2, "completed", "success")])
    assert got["job"] == "succeeded"
    got = _pr(monkeypatch, [_run(2, "completed", "cancelled"), _run(1, "completed", "success")])
    assert got["job"] == "cancelled"  # the newest run of the workflow is the cancelled one


def test_pr_first_workflow_done_while_required_check_is_missing_is_not_terminal(monkeypatch):
    runs = [_run(1, "completed", "success", name="actionlint")]
    got = _pr(monkeypatch, runs, required={"All required checks pass (v2)"}, checks=[_check(1, "actionlint")])
    assert got["job"] in ("queued", "running") and "required" in got["detail"]


def test_pr_required_check_decides_the_verdict(monkeypatch):
    runs = [_run(1, "completed", "success"), _run(2, "in_progress", name="Nightly extras")]
    req = {"All required checks pass (v2)"}
    got = _pr(monkeypatch, runs, required=req, checks=[_check(5, "All required checks pass (v2)")])
    assert got["job"] == "succeeded"  # an optional workflow still running does not hold the verdict
    got = _pr(monkeypatch, runs, required=req,
              checks=[_check(5, "All required checks pass (v2)", conclusion="failure")])
    assert got["job"] == "failed"
    got = _pr(monkeypatch, runs, required=req,
              checks=[_check(5, "All required checks pass (v2)", conclusion="failure"),
                      _check(9, "All required checks pass (v2)", status_="in_progress", conclusion=None)])
    assert got["job"] == "running"  # a rerun of the required check supersedes its failed attempt


def test_pr_without_required_checks_waits_for_the_runs_to_settle(monkeypatch):
    fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 20))
    got = _pr(monkeypatch, [_run(1, "completed", "success", name="actionlint", updated=fresh)])
    assert got["job"] == "running" and "settl" in got["detail"]
    assert _pr(monkeypatch, [_run(1, "completed", "success", name="actionlint")])["job"] == "succeeded"
    assert _pr(monkeypatch, [_run(1, "completed", "failure", name="actionlint")])["job"] == "failed"


def test_pr_poll_reads_through_the_injected_api_and_stops_at_the_deadline(monkeypatch):
    calls: list[str] = []
    api = _api([_run(1, "in_progress")], calls=calls)
    monkeypatch.setattr(kcp, "_gh_api", _api([_run(1, "in_progress")]))  # pr_progress keeps its own reads
    gha._REQUIRED.clear()
    w = gha.poll(f"https://github.com/{REPO}/pull/9", "", now=time.time(), api=api)
    assert w.poll["job"] == "running" and calls
    calls.clear()
    w = gha.poll(f"https://github.com/{REPO}/pull/9", "", now=time.time(), api=api, deadline=time.monotonic() - 1)
    assert w.poll is None and w.progress.deferred and not calls  # nothing recorded, read first next sweep


# --- 7 (case 11): real cadence --------------------------------------------------------------------------------
def test_case11_polls_are_spaced_every_plus_jitter_within_the_budget_and_stop(kanban_home, gh, mono, monkeypatch):
    rands = iter([0.0, 1.0, 0.25, 0.75, 0.5] * 400)
    monkeypatch.setattr(kew, "_rand", lambda: next(rands))
    polled: dict[str, list[float]] = {}
    orig = gh.fetch

    def fetch(repo, run_id):
        polled.setdefault(str(run_id), []).append(mono.now)
        return orig(repo, run_id)
    gh.fetch = fetch
    per_sweep = []
    with kbc.connect_closing() as conn:
        tids = {str(3000 + i): park(conn, 3000 + i) for i in range(8)}
        fast = park(conn, 3999)
        _edit_park(conn, fast, lambda p: p.update(watch={"every": 5}))  # below the floor
        for second in range(0, 3600, 5):
            mono.set(T0 + second)
            before = sum(len(v) for v in polled.values())
            sweep(conn, gh)
            per_sweep.append(sum(len(v) for v in polled.values()) - before)
            if second == 1800:  # one card leaves ``scheduled``
                assert kb.claim_task(conn, tids["3000"], claimer="worker") is None
                with kb.write_txn(conn):
                    conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tids["3000"],))
    assert max(per_sweep) <= kcp.MAX_CARDS_PER_SWEEP                      # budget
    gaps = [b - a for run, ts in polled.items() if run != "3999" for a, b in itertools.pairwise(ts)]
    assert gaps and min(gaps) >= EVERY * 0.9 - 1e-6 and max(gaps) <= EVERY * 1.1 + 5  # every ± 10 % (5 s ticks)
    assert max(gaps) - min(gaps) >= EVERY * 0.1                           # the jitter is real, not a constant
    fast_gaps = [b - a for a, b in itertools.pairwise(polled["3999"])]
    assert fast_gaps and min(fast_gaps) >= ks.MIN_EVERY                    # never under 120 s
    assert max(polled["3000"]) <= T0 + 1800                               # none after leaving scheduled
    assert all(max(ts) > T0 + 3000 for run, ts in polled.items() if run != "3000")


def test_case11_next_due_at_is_the_time_the_sweep_really_polls(kanban_home, gh, mono, monkeypatch):
    monkeypatch.setattr(kew, "_rand", lambda: 1.0)  # +10 %
    with kbc.connect_closing() as conn:
        tid = park(conn)
        sweep(conn, gh)
        due = events(conn, tid, kew.WATCH_EVENT)[-1]["next_due_at"]
        assert due == pytest.approx(T0 + EVERY * 1.1)
        calls = gh.calls
        mono.set(due - 1)
        sweep(conn, gh)
        assert gh.calls == calls
        mono.set(due)
        sweep(conn, gh)
        assert gh.calls > calls


# --- 7 (case 13): a ci park's wake_at deadline still wakes ----------------------------------------------------
def test_case13_external_park_wakes_at_its_deadline_whatever_the_watch_says(kanban_home, gh, clock):
    from hermes_cli.kanban_continuation import pending_wake, wake_due_continuations
    with kbc.connect_closing() as conn:
        deadline = int(time.time()) + 6 * 3600
        until = time.strftime("SCHEDULED_UNTIL=%Y-%m-%dT%H:%M:%SZ CI of the head", time.gmtime(deadline))
        tid = park(conn, reason=until)
        wake_at = pending_wake(conn, tid)["wake_at"]
        assert abs(wake_at - deadline) <= 1
        gh.mode = "5xx"            # GitHub is down the whole time: one card is the blocker's probe...
        other = park(conn, 1002, reason=until)   # ...the other stays parked and red; its deadline still wakes it
        for _ in range(3):
            tick(conn, gh, clock)
        still = [t for t in (tid, other) if status(conn, t) == "scheduled"]
        assert still and kew.card_state(conn, still[0], now=clock.now + 1)["state"] == "external_down"
        assert wake_due_continuations(conn, now=wake_at - 60) == []
        assert set(wake_due_continuations(conn, now=wake_at + 1)) >= set(still)
        assert status(conn, still[0]) == "ready"
        assert kew.card_state(conn, still[0], now=clock.now + 1) is None  # no longer parked: no external state


# --- 7 (case 14): two databases -------------------------------------------------------------------------------
def _replicate_events(source, target, tid):
    """What the Board sync carries for a card: its ``task_events`` rows (ids remapped). ``task_wake_checks`` is
    machine-local and is never copied."""
    known = {tuple(r) for r in target.execute("SELECT kind, payload, created_at FROM task_events WHERE task_id=?",
                                              (tid,))}
    for row in source.execute("SELECT kind, payload, created_at FROM task_events WHERE task_id=? ORDER BY id",
                              (tid,)).fetchall():
        if tuple(row) not in known:
            target.execute("INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                           (tid, *row))
    target.commit()


def test_case14_worker_writes_hub_replica_derives_the_same_state_and_an_old_host_stays_grey(kanban_home, tmp_path):
    t = int(time.time())
    with kbc.connect_closing() as hub:
        _sync_as(hub, "hub")
        new = park(hub, 1, kind="external", ref="a slurm job", wake_check="probe.sh")
        old = park(hub, 2, kind="external", ref="another job", wake_check="probe.sh")
        _edit_park(hub, new, lambda p: p.update(probe_host="worker-a1"))
        _edit_park(hub, old, lambda p: p.update(probe_host="worker-old"))
        # the worker's copy of the board: a second database file with its own machine-local probe rows
        raw = sqlite3.connect(tmp_path / "worker.db")
        hub.backup(raw)
        raw.close()
        worker = kbc.connect(Path(tmp_path / "worker.db"))
        try:
            _sync_as(worker, "worker-a1")
            with kb.write_txn(hub):
                hub.execute("DELETE FROM task_wake_checks")  # the hub runs neither probe
            assert kew.card_state(hub, old, now=t + 10) is None       # nothing heard from either host yet: grey
            assert kew.card_state(hub, new, now=t + 10) is None
            for i, at in enumerate((t, t + 900, t + 1800)):
                assert _shell_apply(worker, new, 1, 'HERMES_WATCH {"job": "running", "service_name": "Slurm"}',
                                    at) is None
                _replicate_events(worker, hub, new)
                mine, theirs = kew.card_state(worker, new, now=at + 30), kew.card_state(hub, new, now=at + 30)
                assert theirs["state"] == mine["state"] == "external_running", i
                assert theirs["external"] == mine["external"] and theirs["external"]["service"] == "Slurm"
            # service trouble seen by the worker reads the same on the hub
            for at in (t + 2700, t + 3600):
                _shell_apply(worker, new, 1, 'HERMES_WATCH {"job": "running", "service": "down", "detail": "502"}', at)
                _replicate_events(worker, hub, new)
            assert kew.card_state(hub, new, now=t + 3630)["state"] == "external_down"
            assert kew.card_state(hub, new, now=t + 3630)["external"] == kew.card_state(
                worker, new, now=t + 3630)["external"]
            _shell_apply(worker, new, 1, 'HERMES_WATCH {"job": "running"}', t + 4500)
            _replicate_events(worker, hub, new)
            assert kew.card_state(hub, new, now=t + 4530)["state"] == "external_running"
            assert len(events(hub, new, kew.RECOVERED_EVENT)) == 1
            # the worker dies: only the hub's own clock moves
            assert kew.card_state(hub, new, now=t + 4500 + 1200)["state"] == "external_unknown"
            got = kew.card_state(hub, new, now=t + 4500 + 1900)
            assert (got["state"], got["health"]) == ("external_down", "host_silent")
            # worker-old never advertised external-watch-v1: its silence stays grey, however long
            assert kew.hosts_seen(hub).keys() == {"worker-a1"}
            for late in (t + 10 * 900, t + 1000 * 900):
                assert kew.card_state(hub, old, now=late) is None
        finally:
            worker.close()


# --- 7: E2E through the dispatcher's wake tick ----------------------------------------------------------------
def test_e2e_timeline_through_dispatcher_ticks(kanban_home, gh, mono, monkeypatch):
    """queued -> running -> outage x2 -> recover -> failed, one ``wake_due_continuations`` (the dispatcher's wake
    step: it starts the due probes and the CI sweep on the probe pool) per 60 s tick, state asserted every tick."""
    from hermes_cli.kanban_continuation import wake_due_continuations

    monkeypatch.setattr(kew, "_rand", lambda: 0.5)
    monkeypatch.setattr(kcp, "_fetch_jobs", gh.fetch)
    monkeypatch.setattr(kcp, "_fetch_runners", lambda repo, labels, deadline=None: [])
    monkeypatch.setattr(kcp, "_gh_api", lambda path: gh.fetch_run(REPO, path.rsplit("/", 1)[-1]))
    seen = []
    with kbc.connect_closing() as conn:
        tid = park(conn)
        db = str(kbc._main_db_file(conn))
        bid = kew.blocker_id("github_run", REPO)

        def dispatcher_tick():
            mono.set(mono.now + 60)
            wake_due_continuations(conn, now=int(mono.now))
            fut = kcp._SWEEPS.get(db)
            if fut is not None:
                fut.result(timeout=60)
            st = kew.card_state(conn, tid, now=mono.now + 1)
            row = (status(conn, tid), st["state"] if st else None, st["external"]["job"] if st else None,
                   st["health"] if st else None)
            seen.append(row)
            return row

        gh.job = "queued"
        assert dispatcher_tick() == ("scheduled", "external_running", "queued", "ok")       # tick 1: first poll
        gh.job = "in_progress"
        for _ in range(4):                                                                   # ticks 2-5: not due
            assert dispatcher_tick() == ("scheduled", "external_running", "queued", "ok")
        assert dispatcher_tick() == ("scheduled", "external_running", "running", "ok")      # tick 6: 300 s later
        gh.mode = "5xx"
        for _ in range(4):
            assert dispatcher_tick() == ("scheduled", "external_running", "running", "ok")
        assert dispatcher_tick() == ("scheduled", "external_unknown", "unknown", "down")    # outage, poll 1
        for _ in range(4):
            assert dispatcher_tick() == ("scheduled", "external_unknown", "unknown", "down")
        assert dispatcher_tick()[0] == "ready"                                              # outage, poll 2: red
        assert events(conn, tid, kew.WATCH_EVENT)[-1]["state"] == "external_down"
        assert ksb.blocker_states(conn)[bid].is_open and len(events(conn, tid, kew.SERVICE_DOWN)) == 1
        gh.mode = None                       # the woken probe card triages; GitHub is back; it re-parks
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.schedule_task(conn, tid, reason="back", wake_check="false", check_every=900,
                                awaiting={"kind": "ci", "ref": url(), "why": "test"})
        assert dispatcher_tick() == ("scheduled", "external_running", "running", "ok")      # recovered
        assert not ksb.blocker_states(conn)[bid].is_open
        gh.job, gh.conclusion = "completed", "failure"
        for _ in range(4):
            assert dispatcher_tick() == ("scheduled", "external_running", "running", "ok")
        assert dispatcher_tick() == ("ready", None, None, None)                             # woken within 1 tick
        met = events(conn, tid, "condition_met")[-1]
        assert met["external_result"]["outcome"] == "failed"
        assert len(events(conn, tid, kew.SERVICE_DOWN)) == 1  # the failed job is a result, not an outage
    assert not [row for row in seen if row[1] == "external_down" and row[0] == "scheduled"]
