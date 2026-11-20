"""External-run cards through the board: sweep -> records -> card state, wakes, the shared blocker. [daveey]

A fake GitHub (200 / 5xx / 429 retry-after / 401 / timeout) is injected through the sweep's ``fetch=`` seam and a
fake wall clock through ``kanban_ci_progress._wall_now``; the plan's cases 1-14 are numbered in the test names.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_ci_progress as kcp
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_external_watch as kew
from hermes_cli import kanban_shared_blockers as ksb
from hermes_cli import kanban_wait_info as kwi
from hermes_cli import kanban_wake_check as kwc
from hermes_cli.kanban_session_wait_rows import _local_wait_info

REPO = "softmax-universe/daveey-hermes"
EVERY = 300
T0 = time.time()


def url(run: int = 1001) -> str:
    return f"https://github.com/{REPO}/actions/runs/{run}"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    monkeypatch.setenv("ASK_GATE_LEDGER", str(home / "state" / "ask-ledger.sqlite"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    yield home
    kwc._shutdown_for_tests()
    for state in (kcp._LAST_CHECK, kcp._LAST_SWEEP, kcp._SWEEPS, kcp._POOL_SCANS, kcp._WARNED_AT):
        state.clear()


class FakeGitHub:
    """Serves jobs/run reads. ``mode`` picks the failure: None (200), 5xx, 429, 401, timeout."""

    def __init__(self):
        self.mode = None
        self.run_modes: dict[str, str] = {}
        self.job = "in_progress"      # in_progress | queued | completed
        self.conclusion = None
        self.calls = 0

    def fetch(self, repo, run_id):
        self.calls += 1
        mode = self.run_modes.get(str(run_id), self.mode)
        if mode == "5xx":
            raise RuntimeError("gh api exit 1: HTTP 502: Bad Gateway (https://api.github.com/)")
        if mode == "429":
            raise RuntimeError("gh api exit 1: HTTP 429: rate limit exceeded retry-after: 700")
        if mode == "401":
            raise kcp.NoCredential("gh api exit 1: HTTP 401: Bad credentials")
        if mode == "timeout":
            raise subprocess.TimeoutExpired("gh", 20)
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30))
        return {"jobs": [{"id": 1, "name": "tests", "status": self.job, "conclusion": self.conclusion,
                          "created_at": created, "labels": ["self-hosted", "ci-fast"],
                          "runner_name": None if self.job == "queued" else "r1"}]}

    def fetch_run(self, repo, run_id):
        return {"status": "completed" if self.job == "completed" else "in_progress", "conclusion": self.conclusion}


@pytest.fixture
def gh():
    return FakeGitHub()


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = T0

        def set(self, t):
            self.now = t
            monkeypatch.setattr(kcp, "_wall_now", lambda: self.now)
    c = Clock()
    c.set(T0)
    return c


def tick(conn, gh, clock, dt=EVERY):
    """One dispatcher sweep ``dt`` s after the last one."""
    clock.set(clock.now + dt)
    kcp._LAST_CHECK.clear()  # the sweep's monotonic spacing is real time; the fake clock drives everything else
    return kcp.sweep(conn, fetch=gh.fetch, fetch_run=gh.fetch_run, fetch_runners=lambda repo: [])


def park(conn, run: int = 1001, *, kind="ci", ref=None, wake_check="false", reason="waiting", **kw) -> str:
    tid = kb.create_task(conn, title=f"card {run}", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    awaiting = {"kind": kind, "ref": ref if ref is not None else url(run), "why": "test"}
    assert kb.schedule_task(conn, tid, reason=reason, wake_check=wake_check, check_every=900, awaiting=awaiting,
                            **kw)
    return tid


def status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]


def state(conn, tid, at=None):
    got = kew.card_state(conn, tid, now=clock_now(at))
    return got


def clock_now(at):
    return at if at is not None else time.time()


def derived(conn, tid, clock):
    return kew.card_state(conn, tid, now=clock.now + 1)


def events(conn, tid, kind):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]


# 1 ---------------------------------------------------------------------------------------------------------
def test_case1_running_run_is_blue_with_every_hover_field(kanban_home, gh, clock):
    with kbc.connect_closing() as conn:
        tid = park(conn)
        assert tick(conn, gh, clock, 0) == []
        got = derived(conn, tid, clock)
        assert (got["state"], got["external"]["job"], got["external"]["health"]) == ("external_running", "running", "ok")
        ext = got["external"]
        assert ext["service"] == "GitHub Actions" and ext["adapter"] == "github_run"
        assert ext["reason"] and ext["url"] == url()
        assert ext["observed_at"] == clock.now and ext["next_due_at"] > ext["observed_at"]
        info = _local_wait_info(conn, [tid], now=clock.now + 1)[tid]
        assert info["wait_state"] == "external_running" and info["external"]["service"] == "GitHub Actions"
        # hover obligation: every field the tooltip reads is present, with check times
        for key in ("service", "job", "health", "reason", "observed_at", "next_due_at"):
            assert ext[key] not in (None, ""), key


# 2 + 3 -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("conclusion,outcome", [("success", "succeeded"), ("failure", "failed")])
def test_case2_3_finished_run_wakes_within_one_tick_and_a_failure_is_never_red(kanban_home, gh, clock, conclusion,
                                                                               outcome):
    with kbc.connect_closing() as conn:
        tid = park(conn)
        tick(conn, gh, clock, 0)
        gh.job, gh.conclusion = "completed", conclusion
        assert tick(conn, gh, clock) == [tid]
        assert status(conn, tid) == "ready"
        met = events(conn, tid, "condition_met")[-1]
        assert met["external_result"]["outcome"] == outcome
        assert met["external_result"]["service"] == "GitHub Actions" and met["external_result"]["url"] == url()
        assert not events(conn, tid, kew.SERVICE_DOWN) and not events(conn, tid, kew.RECOVERED_EVENT)
        assert all(r["state"] != "external_down" for r in events(conn, tid, kew.WATCH_EVENT))


# 4 ---------------------------------------------------------------------------------------------------------
def test_case4_one_5xx_is_unknown_then_recovers(kanban_home, gh, clock):
    with kbc.connect_closing() as conn:
        tid = park(conn)
        tick(conn, gh, clock, 0)
        gh.mode = "5xx"
        assert tick(conn, gh, clock) == []
        got = derived(conn, tid, clock)
        assert (got["state"], got["health"]) == ("external_unknown", "down")
        assert got["external"]["service"] == "GitHub Actions" and got["external"]["reason"]
        gh.mode = None
        tick(conn, gh, clock)
        assert derived(conn, tid, clock)["state"] == "external_running"
        assert status(conn, tid) == "scheduled"


# 5 + 6 -----------------------------------------------------------------------------------------------------
def test_case5_6_two_5xx_are_red_with_one_blocker_for_five_cards_and_recovery_clears_it(kanban_home, gh, clock):
    with kbc.connect_closing() as conn:
        tids = [park(conn, 2000 + i) for i in range(5)]
        tick(conn, gh, clock, 0)
        gh.mode = "5xx"
        tick(conn, gh, clock)
        assert {derived(conn, t, clock)["state"] for t in tids} == {"external_unknown"}
        woken = tick(conn, gh, clock)
        bid = kew.blocker_id("github_run", REPO)
        states = {k: v for k, v in ksb.blocker_states(conn).items() if k.startswith("service-down")}
        assert list(states) == [bid], states  # ONE blocker for five cards
        assert len(woken) == 1 and status(conn, woken[0]) == "ready"  # one probe card woken
        probe = woken[0]
        waiting = [t for t in tids if t != probe]
        assert len(waiting) == 4
        for t in waiting:
            got = derived(conn, t, clock)
            assert got["state"] == "external_down" and got["health"] == "down"
            assert got["external"]["blocker_id"] == bid and got["external"]["triage_card"] == probe
            assert status(conn, t) == "scheduled"
        assert states[bid].is_open
        # recovery: first ok poll clears the blocker and writes external_recovered on each red card
        gh.mode = None
        tick(conn, gh, clock)
        assert not ksb.blocker_states(conn).get(bid) or not ksb.blocker_states(conn)[bid].is_open
        for t in waiting:
            assert derived(conn, t, clock)["state"] == "external_running"
            assert len(events(conn, t, kew.RECOVERED_EVENT)) == 1


# 7 ---------------------------------------------------------------------------------------------------------
def test_case7_429_never_counts_toward_red_and_honours_retry_after(kanban_home, gh, clock):
    with kbc.connect_closing() as conn:
        tid = park(conn)
        tick(conn, gh, clock, 0)
        gh.mode = "429"
        tick(conn, gh, clock)
        calls = gh.calls
        for _ in range(2):  # 300 s later each: still inside retry-after 700 s, so GitHub is not asked again
            tick(conn, gh, clock)
        assert gh.calls == calls
        got = derived(conn, tid, clock)
        assert (got["state"], got["health"]) == ("external_unknown", "rate_limited")
        for _ in range(6):
            tick(conn, gh, clock, 800)
            assert derived(conn, tid, clock)["state"] != "external_down"
        assert status(conn, tid) == "scheduled" and not events(conn, tid, kew.SERVICE_DOWN)


# 8 ---------------------------------------------------------------------------------------------------------
def test_case8_401_is_red_at_once_without_waking(kanban_home, gh, clock):
    with kbc.connect_closing() as conn:
        tid = park(conn)
        gh.mode = "401"
        tick(conn, gh, clock, 0)
        got = derived(conn, tid, clock)
        assert (got["state"], got["health"]) == ("external_down", "auth_failed")
        assert status(conn, tid) == "scheduled"


# 9 ---------------------------------------------------------------------------------------------------------
def test_case9_silent_host_is_unknown_after_one_interval_and_red_after_two(kanban_home, gh, clock):
    with kbc.connect_closing() as conn:
        tid = park(conn)
        tick(conn, gh, clock, 0)  # the last record the host ever writes
        t = clock.now
        assert kew.card_state(conn, tid, now=t + 100)["state"] == "external_running"
        assert kew.card_state(conn, tid, now=t + 1.5 * EVERY)["state"] == "external_unknown"
        assert kew.card_state(conn, tid, now=t + 2.5 * EVERY)["state"] == "external_down"
        assert kew.card_state(conn, tid, now=t + 2.5 * EVERY)["health"] == "host_silent"


# 10 --------------------------------------------------------------------------------------------------------
def _shell_apply(conn, tid, rc, out):
    st = kwc.get_state(conn, tid)
    return kwc.apply_result(conn, tid, st["cmd"], rc, out, generation=st["generation"])


def test_case10_legacy_return_codes_and_hermes_watch_lines(kanban_home):
    with kbc.connect_closing() as conn:
        a = park(conn, kind="external", ref="a slurm job", wake_check="probe.sh")
        assert _shell_apply(conn, a, 1, "") is None
        got = kew.card_state(conn, a)
        assert (got["state"], got["external"]["adapter"], got["external"]["job"]) == ("external_running", "shell", "running")
        b = park(conn, 1, kind="external", ref="a slurm job", wake_check="probe.sh")
        _shell_apply(conn, b, 1, 'HERMES_WATCH {"job": "running", "service": "down", "detail": "502"}')
        assert kew.card_state(conn, b)["state"] == "external_unknown"
        _shell_apply(conn, b, 1, 'HERMES_WATCH {"job": "running", "service": "down", "detail": "502"}')
        assert kew.card_state(conn, b)["state"] == "external_down"
        c = park(conn, 2, kind="external", ref="a slurm job", wake_check="probe.sh")
        assert _shell_apply(conn, c, 1, 'HERMES_WATCH {"job": "failed", "detail": "oom"}') == "condition_met"
        assert events(conn, c, "condition_met")[-1]["external_result"]["outcome"] == "failed"
        d = park(conn, 3, kind="external", ref="a slurm job", wake_check="probe.sh")
        assert _shell_apply(conn, d, 0, "") == "condition_met"  # legacy exit 0 still wakes, result unknown
        assert events(conn, d, "condition_met")[-1]["external_result"]["outcome"] == "done_unknown"
        e = park(conn, 4, kind="external", ref="a slurm job", wake_check="probe.sh")
        _shell_apply(conn, e, 3, "stale")
        assert kew.card_state(conn, e)["state"] == "external_unknown"


# 11 --------------------------------------------------------------------------------------------------------
def test_case11_cadence_budget_and_stop_after_leaving_scheduled(kanban_home, gh, clock):
    with kbc.connect_closing() as conn:
        tids = [park(conn, 3000 + i) for i in range(8)]
        tick(conn, gh, clock, 0)
        assert gh.calls <= kcp.MAX_CARDS_PER_SWEEP * 2  # the per-sweep card budget still bounds the reads
        first = gh.calls
        # one interval later the same cards are read again, never sooner
        kcp._LAST_CHECK.clear()
        kcp.sweep(conn, fetch=gh.fetch, fetch_run=gh.fetch_run, fetch_runners=lambda repo: [])
        gh.job, gh.conclusion = "completed", "success"
        woken = tick(conn, gh, clock)
        assert woken and all(status(conn, t) == "ready" for t in woken)
        calls = gh.calls
        tick(conn, gh, clock)
        tick(conn, gh, clock)
        left = [t for t in tids if status(conn, t) == "scheduled"]
        assert gh.calls - calls <= 2 * 2 * max(1, len(left)) + 2  # woken cards are never polled again
        assert first > 0


def test_case11_declared_every_has_a_floor(kanban_home, gh, clock):
    with kbc.connect_closing() as conn:
        tid = park(conn)
        with kb.write_txn(conn):
            row = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='scheduled'",
                               (tid,)).fetchone()
            payload = json.loads(row[1] or "{}")
            payload["watch"] = {"every": 5}
            conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row[0]))
        assert kcp._declared_every(conn, tid) == 120


# 12 --------------------------------------------------------------------------------------------------------
def test_case12_ci_park_nothing_watches_is_red(kanban_home):
    with kbc.connect_closing() as conn:
        tid = park(conn)  # a legacy park: its ref names no run and nothing probes it
        with kb.write_txn(conn):
            row = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='scheduled'",
                               (tid,)).fetchone()
            payload = json.loads(row[1] or "{}")
            payload["awaiting"]["ref"] = "a release train"
            payload.pop("wake_check", None)
            conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row[0]))
            conn.execute("DELETE FROM task_wake_checks WHERE task_id=?", (tid,))
        got = kew.card_state(conn, tid)
        assert (got["state"], got["health"]) == ("external_down", "watch_missing")
        assert got["external"]["service"] and got["external"]["reason"]
        assert _local_wait_info(conn, [tid])[tid]["wait_state"] == "external_down"


# 13 --------------------------------------------------------------------------------------------------------
def test_case13_a_deadline_park_still_wakes_and_is_not_external(kanban_home):
    with kbc.connect_closing() as conn:
        tid = park(conn, kind="clock", ref="soak", wake_check=None, clock_only=True)
        assert kew.card_state(conn, tid) is None
        from hermes_cli.kanban_continuation import wake_due_continuations
        assert tid in wake_due_continuations(conn, now=int(time.time()) + 10 * 24 * 3600)


# 14 --------------------------------------------------------------------------------------------------------
def test_case14_old_host_silence_stays_grey_and_an_advertising_host_goes_red(kanban_home, gh, clock):
    with kbc.connect_closing() as conn:
        old, new = park(conn, 4001), park(conn, 4002)
        with kb.write_txn(conn):
            for tid, host in ((old, "old-host"), (new, "new-host")):
                row = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='scheduled'",
                                   (tid,)).fetchone()
                payload = json.loads(row[1] or "{}")
                payload["probe_host"] = host
                conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row[0]))
            # new-host wrote one watch record at T0 (as a synced event would arrive); old-host never has
            kb._append_event(conn, new, kew.WATCH_EVENT, {"adapter": "github_run", "scope": REPO, "host": "new-host",
                                                          "job": "running", "service": "ok", "observed_at": T0,
                                                          "next_due_at": T0 + EVERY, "every": EVERY,
                                                          "consecutive_failures": 0})
        late = T0 + 5 * EVERY
        assert kew.card_state(conn, old, now=late) is None  # grey: that host never advertised watching
        got = kew.card_state(conn, new, now=late)
        assert (got["state"], got["health"]) == ("external_down", "host_silent")


# E2E -------------------------------------------------------------------------------------------------------
def test_e2e_timeline_queued_running_outage_recover_failed(kanban_home, gh, clock):
    with kbc.connect_closing() as conn:
        tid = park(conn)
        bid = kew.blocker_id("github_run", REPO)
        gh.job = "queued"
        tick(conn, gh, clock, 0)
        assert (derived(conn, tid, clock)["state"], derived(conn, tid, clock)["external"]["job"]) == (
            "external_running", "queued")
        gh.job = "in_progress"
        tick(conn, gh, clock)
        assert derived(conn, tid, clock)["external"]["job"] == "running"
        gh.mode = "5xx"
        tick(conn, gh, clock)
        assert derived(conn, tid, clock)["state"] == "external_unknown"
        assert status(conn, tid) == "scheduled"
        assert tick(conn, gh, clock) == [tid]  # second consecutive failure: red, woken as the service probe
        assert status(conn, tid) == "ready"
        assert events(conn, tid, kew.WATCH_EVENT)[-1]["state"] == "external_down"
        assert ksb.blocker_states(conn)[bid].is_open
        # the woken probe triages, GitHub is back, it re-parks on the same run
        gh.mode = None
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.schedule_task(conn, tid, reason="back", wake_check="false", check_every=900,
                                awaiting={"kind": "ci", "ref": url(), "why": "test"})
        tick(conn, gh, clock)
        assert derived(conn, tid, clock)["state"] == "external_running"
        assert not ksb.blocker_states(conn).get(bid) or not ksb.blocker_states(conn)[bid].is_open
        gh.job, gh.conclusion = "completed", "failure"
        assert tick(conn, gh, clock) == [tid]
        assert events(conn, tid, "condition_met")[-1]["external_result"]["outcome"] == "failed"
        assert status(conn, tid) == "ready"
