"""Review round 2 of the external-run cards (PR #3745 @3c8f438b): a poll read for a park the card has since
left, an outage whose first healthy read is a finished job, and a shell probe's Retry-After. Disposable fixture
boards, a fake GitHub and the public lifecycle APIs only. The first four cases are the reviewer's repro file."""
import pytest

from hermes_cli import kanban_ci_progress as kcp
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_external_watch as kew
from hermes_cli import kanban_shared_blockers as ksb
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli import test_kanban_external_watch as h

kanban_home, gh, clock = h.kanban_home, h.gh, h.clock


# --- an outage clears on the first healthy read, a finished job's included -----------------------------------

@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_terminal_recovery_clears_outage(kanban_home, gh, clock, conclusion):
    with kbc.connect_closing() as conn:
        tids = [h.park(conn, 8001), h.park(conn, 8002)]
        gh.mode = "5xx"
        h.tick(conn, gh, clock)
        probe = h.tick(conn, gh, clock)
        assert len(probe) == 1
        bid = kew.blocker_id("github_run", h.REPO)
        assert ksb.blocker_states(conn)[bid].is_open
        gh.mode = None
        gh.job, gh.conclusion = "completed", conclusion
        (other,) = [t for t in tids if t not in probe]
        assert h.tick(conn, gh, clock) == [other]
        assert not ksb.blocker_states(conn)[bid].is_open
        woke = h.events(conn, other, kwc.CONDITION_MET_EVENT)[-1]["external_result"]  # the wake is kept
        assert woke["outcome"] == ("succeeded" if conclusion == "success" else "failed")


# --- a poll belongs to the park it was read for ---------------------------------------------------------------

def _repark(conn, tid, run):
    """An ordinary wake, claim and re-park, as another actor commits while a remote read is in flight."""
    st = kwc.get_state(conn, tid)
    assert kwc.apply_result(conn, tid, st["cmd"], 0, "", generation=st["generation"])
    assert kb.claim_task(conn, tid, claimer="worker")
    assert kb.schedule_task(conn, tid, reason="replacement run", wake_check="false", check_every=900,
                            awaiting={"kind": "ci", "ref": h.url(run), "why": "replacement"})


@pytest.mark.parametrize("replacement", [8102, 8101], ids=["another_run", "same_ref_new_park"])
def test_sweep_discards_result_from_previous_park(kanban_home, gh, clock, replacement):
    with kbc.connect_closing() as conn:
        tid = h.park(conn, 8101)
        reads = []

        def fetch(repo, run_id):
            if not reads:
                _repark(conn, tid, replacement)
                gh.job, gh.conclusion = "completed", "success"
            reads.append(str(run_id))
            return gh.fetch(repo, run_id)

        assert kcp.sweep(conn, fetch=fetch, fetch_run=gh.fetch_run, fetch_runners=lambda repo: []) == []
        assert h.status(conn, tid) == "scheduled"
        assert not h.events(conn, tid, kew.WATCH_EVENT)
        # The replacement park is read on its own: the next sweep wakes it with its own run's result.
        clock.set(clock.now + h.EVERY)
        kcp._LAST_CHECK.clear()
        assert kcp.sweep(conn, fetch=fetch, fetch_run=gh.fetch_run, fetch_runners=lambda repo: []) == [tid]
        assert reads == ["8101", str(replacement)]
        assert h.status(conn, tid) == "ready"


def _finished():
    return {"job": "succeeded", "service": "ok", "detail": "done", "url": h.url(8201), "retry_after": None,
            "terminal": True}


def test_apply_poll_checks_the_park_id_and_the_ref_separately(kanban_home):
    with kbc.connect_closing() as conn:
        tid = h.park(conn, 8201)
        park_id = kew.park_context(conn, tid)["event_id"]
        kw = {"now": h.T0, "adapter": "github_run", "scope": h.REPO}
        assert kew.apply_poll(conn, tid, _finished(), park_id=park_id - 1, ref=h.url(8201), **kw) is None
        assert kew.apply_poll(conn, tid, _finished(), park_id=park_id, ref=h.url(8299), **kw) is None
        assert kew.apply_poll(conn, tid, _finished(), park_id=None, ref=h.url(8299), **kw) is None
        assert h.status(conn, tid) == "scheduled" and not h.events(conn, tid, kew.WATCH_EVENT)
        assert kew.apply_poll(conn, tid, _finished(), park_id=park_id, ref=h.url(8201), **kw)
        assert h.status(conn, tid) == "ready"


def test_a_card_with_no_park_matches_no_poll(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="never parked", assignee="worker")
        assert kew._same_park(conn, tid, 1, None) is False
        assert kew._same_park(conn, tid, None, None) is True  # a caller that names no park is not checked


# --- a shell probe's Retry-After moves the real next probe ----------------------------------------------------

def _probe(conn, tid, out, rc=1, **kw):
    st = kwc.get_state(conn, tid)
    now = int(h.T0)
    assert kwc.apply_result(conn, tid, st["cmd"], rc, out, generation=st["generation"], now=now, **kw) is None
    after = kwc.get_state(conn, tid)
    assert after["generation"] == st["generation"]
    return after["next_at"] - now


def _external(conn):
    return h.park(conn, kind="external", ref="remote job", wake_check="probe.sh")


def test_shell_retry_after_controls_real_next_probe(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _external(conn)
        assert _probe(conn, tid, 'HERMES_WATCH {"service":"rate_limited","retry_after":3600}') == 3600


def test_shell_retry_after_applies_to_a_could_not_tell_exit(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _external(conn)
        assert _probe(conn, tid, 'HERMES_WATCH {"service":"rate_limited","retry_after":3600}', rc=3) == 3600


def test_shell_retry_after_never_shortens_the_cadence(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _external(conn)
        assert _probe(conn, tid, 'HERMES_WATCH {"service":"rate_limited","retry_after":60}') == 900


def test_shell_retry_after_is_ignored_unless_rate_limited(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _external(conn)
        assert _probe(conn, tid, 'HERMES_WATCH {"service":"ok","job":"running","retry_after":3600}') == 900


def test_shell_retry_after_keeps_an_expedite(kanban_home):
    """A wait expedite that committed while the probe ran (next_at moved before the claim's slot) still wins."""
    with kbc.connect_closing() as conn:
        tid = _external(conn)
        now = int(h.T0)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_wake_checks SET next_at = ? WHERE task_id = ?", (now - 5, tid))
        assert _probe(conn, tid, 'HERMES_WATCH {"service":"rate_limited","retry_after":3600}',
                      claimed_next_at=now + 900) == -5
