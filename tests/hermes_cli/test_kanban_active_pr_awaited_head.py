"""Review nits on the awaited-review release (PR #1995, review 5888333701). [daveey]

1. A cached head never releases: a force-push after the approval must not match the
   approved sha from a head read up to OPEN_TTL_SECONDS old.
2. The head comes from ``pr_state``'s own ``gh pr view`` (one read, one cache entry).
3. One "parked run" definition: a run that wrote a park event but ended crashed/reclaimed
   (or never ended) is not parked, for the same-second tie as for the awaited review.
4. Only a bare leading ``hermes kanban wait-check task <id>`` or ``awaiting`` kind ``card``
   with a task-id ref names an awaited card; wrapped probes and gateway request refs hold.
"""
import json
import subprocess

import pytest

from hermes_cli import kanban_awaited_review as kar
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch_pr_state as prs
from tests.hermes_cli import test_kanban_active_pr_awaited_review as base
from tests.hermes_cli import test_kanban_park_invariants as park_invariants

kanban_home = park_invariants.kanban_home
clock = base.clock
pytestmark = pytest.mark.usefixtures("kanban_home")
URL, HEAD, APPROVE = base.URL, base.HEAD, base.APPROVE
PUSHED = "b" * 40
_REAL_GH_PR_STATE = prs._gh_pr_state
_REAL_PR_HEAD = kar.pr_head


@pytest.fixture
def gh(monkeypatch):
    """Fake ``gh pr view``: ``gh.head`` is the PR's current head; ``gh.calls`` the argv seen."""
    class Gh:
        head = HEAD
        calls: list = []

    Gh.calls = []

    def run(argv, **kwargs):
        Gh.calls.append(list(argv))
        out = {"state": "OPEN", "headRefOid": Gh.head, "comments": []}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(out), stderr="")

    monkeypatch.setattr(prs, "_gh_pr_state", _REAL_GH_PR_STATE)
    monkeypatch.setattr(kar, "pr_head", _REAL_PR_HEAD)
    monkeypatch.setattr(subprocess, "run", run)
    prs.clear_cache()
    kar.clear_cache()
    yield Gh
    prs.clear_cache()
    kar.clear_cache()


# --- 1. a cached head never releases ------------------------------------------------------

def _age_head_reads(seconds=60):
    """Move every cached head read ``seconds`` into the past (an earlier dispatcher tick).

    Both cache shapes store a monotonic instant second (read time here, expiry in the old
    per-module cache), so shifting it back means "that read happened earlier" either way."""
    for cache in (getattr(prs, "_heads", None), getattr(kar, "_heads", None)):
        for key, (head, at) in list((cache or {}).items()):
            cache[key] = (head, at - seconds)  # type: ignore[index]


def test_force_push_after_a_cached_head_read_keeps_the_hold(clock, monkeypatch, gh):
    with kbc.connect() as conn:
        tid, _ = base._awaiting_owner(conn, clock, monkeypatch, APPROVE)
        assert kar.pr_head(URL) == HEAD  # an earlier tick read the approved head
        _age_head_reads()
        gh.head = PUSHED  # the owner force-pushed after the approval
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) == "active_pr"


def test_approval_at_the_current_head_still_releases_through_real_gh(clock, monkeypatch, gh):
    with kbc.connect() as conn:
        tid, _ = base._awaiting_owner(conn, clock, monkeypatch, APPROVE)
        assert kar.pr_head(URL) == HEAD
        _age_head_reads()
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) is None


def test_a_cold_release_costs_one_read(clock, monkeypatch, gh):
    with kbc.connect() as conn:
        tid, _ = base._awaiting_owner(conn, clock, monkeypatch, APPROVE)
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) is None
        assert len(gh.calls) == 1


def test_a_failed_release_reread_keeps_the_hold(clock, monkeypatch, gh):
    """The cached approved head is dropped before the re-read: a gh failure then is unknown."""
    with kbc.connect() as conn:
        tid, _ = base._awaiting_owner(conn, clock, monkeypatch, APPROVE)
        assert kar.pr_head(URL) == HEAD
        _age_head_reads()
        monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "down"))
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) == "active_pr"


def test_a_cached_head_that_mismatches_refuses_without_a_new_read(clock, monkeypatch, gh):
    with kbc.connect() as conn:
        tid, _ = base._awaiting_owner(conn, clock, monkeypatch, APPROVE)
        gh.head = PUSHED
        assert kar.pr_head(URL) == PUSHED
        _age_head_reads()
        reads = len(gh.calls)
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) == "active_pr"
        assert len(gh.calls) == reads  # a refusal may come from the cache


# --- 2. one gh read, one cache entry --------------------------------------------------------

def test_pr_head_shares_pr_states_read(gh):
    assert prs.pr_state(URL) == "OPEN"
    assert kar.pr_head(URL) == HEAD
    assert prs.pr_state(URL) == "OPEN"
    assert len(gh.calls) == 1
    assert gh.calls[0][-1] == "state,comments,headRefOid,baseRefName,body"


def test_pr_state_reuses_pr_heads_read(gh):
    assert kar.pr_head(URL) == HEAD
    assert prs.pr_state(URL) == "OPEN"
    assert len(gh.calls) == 1


# --- 3. one "parked run" definition ---------------------------------------------------------

@pytest.mark.parametrize("outcome", ["crashed", "reclaimed", "timed_out", None])
@pytest.mark.parametrize("kind", ["scheduled", "continued"])
def test_same_second_tie_of_a_run_that_did_not_end_parked_still_guards(clock, monkeypatch, kind, outcome):
    """The run commented, wrote its park event, then crashed/was reclaimed in that second."""
    with kbc.connect() as conn:
        tid, run_id = base._owner(conn, monkeypatch)
        base._comment(conn, tid, run_id, f"Opened {URL}; waiting on review", monkeypatch)
        base._park(conn, tid, kind)
        base._wake(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET outcome=?, ended_at=CASE WHEN ? THEN ended_at END WHERE id=?",
                         (outcome or kind, outcome is not None, run_id))
        assert prs.active_pr_reason(conn, tid, clock[0] + 700) == "active_pr"


@pytest.mark.parametrize("kind", ["scheduled", "continued"])
def test_same_second_tie_needs_the_park_event_its_outcome_names(clock, monkeypatch, kind):
    """A ``continued`` run carrying only a ``scheduled`` event (or vice versa) is not that park."""
    other = {"scheduled": "continued", "continued": "scheduled"}[kind]
    with kbc.connect() as conn:
        tid, run_id = base._owner(conn, monkeypatch)
        base._comment(conn, tid, run_id, f"Opened {URL}", monkeypatch)
        base._park(conn, tid, kind)
        base._wake(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET outcome=? WHERE id=?", (other, run_id))
        assert prs.active_pr_reason(conn, tid, clock[0] + 700) == "active_pr"


def test_latest_parked_run_is_the_one_definition(clock, monkeypatch):
    from hermes_cli.kanban_pr_same_run_park import latest_parked_run

    with kbc.connect() as conn:
        tid, run_id = base._owner(conn, monkeypatch)
        assert latest_parked_run(conn, tid) is None  # running
        base._park(conn, tid, "scheduled")
        assert latest_parked_run(conn, tid)["id"] == run_id
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET outcome='crashed' WHERE id=?", (run_id,))
        assert latest_parked_run(conn, tid) is None


# --- 4. wrapped probes and gateway request refs hold (fail closed) ----------------------------

WRAPPED = [
    "bash -lc 'hermes kanban wait-check task {rev}'",
    "timeout 30 hermes kanban wait-check task {rev}",
    "~/.hermes/bin/hermes kanban wait-check task {rev}",
    "/usr/bin/env hermes kanban wait-check task {rev}",
    "HERMES_HOME=/x hermes kanban wait-check task {rev}",
    "test -f /x || hermes kanban wait-check task {rev}",
    "hermes kanban wait-check task {rev} && false",
    "hermes kanban wait-check task {rev} | grep -q done",
    "hermes kanban wait-check task {rev}\nfalse",
]


@pytest.mark.parametrize("probe", WRAPPED)
def test_wrapped_wait_check_names_no_awaited_card(probe):
    assert kar.awaited_cards({"wake_check": probe.format(rev="t_abc")}) == []
    assert kar.awaited_cards({"_wake_episode_v1": {"park_cmd": probe.format(rev="t_abc")}}) == []


@pytest.mark.parametrize("awaiting", [
    {"kind": "card", "ref": "worker-3/request/01a1d4"},
    {"kind": "card", "ref": "worker-3/request/t_abc"},
    {"kind": "card", "ref": "worker-3/t_abc", "gateway": "worker-3"},
    [{"kind": "card", "ref": "t_abc"}],
], ids=["request", "request_task_suffix", "gateway_prefixed", "list"])
def test_gateway_request_and_list_awaiting_name_no_awaited_card(awaiting):
    assert kar.awaited_cards({"awaiting": awaiting}) == []


@pytest.mark.parametrize("probe", WRAPPED[:4])
def test_wrapped_probe_park_keeps_the_hold_after_an_approval(clock, monkeypatch, probe):
    with kbc.connect() as conn:
        tid, rev = base._awaiting_owner(conn, clock, monkeypatch, APPROVE)
        _rewrite_park(conn, tid, lambda p: _without(p, "awaiting") | {"wake_check": probe.format(rev=rev)})
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) == "active_pr"


def test_gateway_request_awaiting_park_keeps_the_hold_after_an_approval(clock, monkeypatch):
    with kbc.connect() as conn:
        tid, rev = base._awaiting_owner(conn, clock, monkeypatch, APPROVE, via="awaiting")
        _rewrite_park(conn, tid, lambda p: _without(p, "wake_check")
                      | {"awaiting": {"kind": "card", "ref": f"worker-3/request/{rev}"}})
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) == "active_pr"


@pytest.mark.parametrize("change", ["wake_check", "awaiting"])
def test_rewritten_park_fixture_releases_with_the_bare_card_wait(clock, monkeypatch, change):
    """Control for the two tests above: the same rewrite naming the card plainly releases."""
    with kbc.connect() as conn:
        tid, rev = base._awaiting_owner(conn, clock, monkeypatch, APPROVE)
        if change == "wake_check":
            _rewrite_park(conn, tid, lambda p: _without(p, "awaiting")
                          | {"wake_check": f"hermes kanban wait-check task {rev} --gateway worker-3 --from running"})
        else:
            _rewrite_park(conn, tid, lambda p: _without(p, "wake_check")
                          | {"awaiting": {"kind": "card", "ref": rev}})
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) is None


def test_a_wrapped_probe_parked_for_real_is_named_by_its_recorded_awaiting(clock, monkeypatch):
    """The park records ``awaiting`` kind ``card`` for a live card its probe names, wrapped or
    not, so that record (not the probe parser) is what names the review card."""
    with kbc.connect() as conn:
        rev = kb.create_task(conn, title="exact-head review", assignee="reviewer")
        tid, _ = base._owner(conn, monkeypatch)
        assert kb.schedule_task(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting on review",
                                wake_check=f"timeout 30 hermes kanban wait-check task {rev}", check_every=900)
        payload = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='scheduled'",
                                          (tid,)).fetchone()[0])
        assert kar.awaited_cards(_without(payload, "awaiting")) == []
        assert kar.awaited_cards(payload) == [rev]


def _without(payload, key):
    return {k: v for k, v in payload.items() if k != key}


def _rewrite_park(conn, tid, change):
    with kb.write_txn(conn):
        row = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='scheduled'",
                           (tid,)).fetchone()
        payload = change(json.loads(row["payload"]))
        payload.pop("_wake_episode_v1", None)
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
