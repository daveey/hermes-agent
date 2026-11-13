"""A park that itself names its PR covers the parking run's own note that first names it. [daveey]

Ship's request-review order: open PR N, park on its CI (``awaiting.ref`` = PR N, wake_check
``hermes-core-wt-land --review-ci-probe --repo O/R --number N``), then post the summary note
that is the card's first comment naming PR N (2026-11-13 MacBook t_01a1cff0541bf30c: park
04:26:18 on #2208, own note 04:26:25, ``condition_met`` 04:37:13, held ``active_pr``).
"""
import time

import pytest

from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli.kanban_pr_park_note import park_named_prs
from tests.hermes_cli.test_kanban_park_invariants import kanban_home

__all__ = ["kanban_home"]
pytestmark = pytest.mark.usefixtures("kanban_home")
REPO = "softmax-universe/daveey-hermes"
SHA = "b4ae3c79bf" + "0" * 30
PR = f"https://github.com/{REPO}/pull/2208"
OTHER = f"https://github.com/{REPO}/pull/2209"
PROBE = f"/bin/true --review-ci-probe --repo {REPO} --number 2208 --sha {SHA}"


def _comment(conn, tid, run_id, monkeypatch, body):
    with monkeypatch.context() as env:
        env.setenv("HERMES_KANBAN_TASK", tid)
        env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
        return kb.add_comment(conn, tid, "a", body)


def _park(conn, tid, kind, *, ref=PR, wake_check=PROBE, reason=None):
    awaiting = {"kind": "ci", "ref": ref, "why": f"required checks on head {SHA}"} if ref else None
    reason = reason or "review deferred: CI pending; rerun ship with --request-review when CI finishes"
    if kind == "scheduled":
        assert kb.schedule_task(conn, tid, reason=reason, wake_check=wake_check, check_every=120, awaiting=awaiting)
    else:
        assert kc.continue_task(conn, tid, checkpoint=reason, resume_after_seconds=3600,
                                wake_check=wake_check, awaiting=awaiting)["ok"]


def _replay(conn, monkeypatch, now, *, kind="scheduled", note=f"Opened {PR}; parked on its CI", writer="own",
            synced=True, **park):
    """claimed -> park naming PR -> note first naming PR (+7 s) -> condition_met (+655 s)."""
    if synced:
        conn.execute("ALTER TABLE task_runs ADD COLUMN uid TEXT")
    tid = kb.create_task(conn, title="G3 core fix", assignee="a")
    kb.claim_task(conn, tid)
    run_id = conn.execute("SELECT id FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0]
    if synced:
        conn.execute("UPDATE task_runs SET uid = '01a1d4d2-b4be-7e51-80ce-' || printf('%012d', id) WHERE id=?",
                     (run_id,))
    _comment(conn, tid, run_id, monkeypatch, "Working on the fix; no PR yet")
    monkeypatch.setattr(time, "time", lambda: now + 1)
    _park(conn, tid, kind, **park)
    monkeypatch.setattr(time, "time", lambda: now + 8)
    if writer == "own":
        _comment(conn, tid, run_id, monkeypatch, note)
    else:  # another run of the same card (a lingering earlier/later process)
        conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?, 'a', 'crashed', ?)",
                     (tid, now))
        other = conn.execute("SELECT MAX(id) FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0]
        if synced:
            conn.execute("UPDATE task_runs SET uid = '01a1d4d2-0000-7000-8000-' || printf('%012d', id) WHERE id=?",
                         (other,))
        _comment(conn, tid, other, monkeypatch, note)
    monkeypatch.setattr(time, "time", lambda: now + 655)
    kb._append_event(conn, tid, "condition_met", {"stdout": f"state=green sha={SHA}"})
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    return tid


@pytest.fixture
def now(monkeypatch):
    t = int(time.time())
    monkeypatch.setattr(time, "time", lambda: t)
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")
    return t


def _held(conn, tid, now):
    return prs.active_pr_reason(conn, tid, now + 661), kbd.check_respawn_guard(conn, tid)


@pytest.mark.parametrize("synced", [True, False], ids=["synced", "local"])
@pytest.mark.parametrize("kind", ["scheduled", "continued"])
def test_t1_ship_request_review_park_then_own_note_first_naming_pr_is_not_held(monkeypatch, now, kind, synced):
    with kbc.connect() as conn:
        tid = _replay(conn, monkeypatch, now, kind=kind, synced=synced)
        assert _held(conn, tid, now) == (None, None)


@pytest.mark.parametrize("park", [
    {"ref": PR, "wake_check": None},  # awaiting.ref alone
    {"ref": None, "wake_check": PROBE},  # the review-ci-probe alone
    {"ref": None, "wake_check": None, "reason": f"waiting on CI of {PR}"},  # the reason alone
], ids=["awaiting_ref", "wake_check_probe", "reason_url"])
def test_t1_each_park_field_alone_names_the_pr(monkeypatch, now, park):
    with kbc.connect() as conn:
        tid = _replay(conn, monkeypatch, now, **{**park, "wake_check": park["wake_check"] or "/bin/true"})
        assert _held(conn, tid, now) == (None, None)


def test_t2_note_also_naming_a_pr_the_park_does_not_name_is_held(monkeypatch, now):
    with kbc.connect() as conn:
        tid = _replay(conn, monkeypatch, now, note=f"Opened {PR}; also opened {OTHER}")
        assert _held(conn, tid, now) == ("active_pr", "active_pr")


def test_t2_park_naming_nothing_keeps_the_hold(monkeypatch, now):
    with kbc.connect() as conn:
        tid = _replay(conn, monkeypatch, now, ref=None, wake_check="/bin/true",
                      reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting")
        assert _held(conn, tid, now) == ("active_pr", "active_pr")


@pytest.mark.parametrize("synced", [True, False], ids=["synced", "local"])
def test_t3_note_written_by_another_run_is_held(monkeypatch, now, synced):
    with kbc.connect() as conn:
        tid = _replay(conn, monkeypatch, now, writer="other", synced=synced)
        assert _held(conn, tid, now) == ("active_pr", "active_pr")


@pytest.mark.parametrize("payload, expected", [
    ({"awaiting": {"kind": "ci", "ref": PR}}, {PR}),
    ({"awaiting": {"kind": "ci", "ref": PR + "/"}}, {PR}),
    ({"awaiting": {"kind": "ci", "ref": f"{PR}/files"}}, set()),  # not a bare PR URL
    ({"awaiting": {"kind": "ci", "ref": f"see {PR}"}}, set()),
    ({"awaiting": {"kind": "card", "ref": "t_01a1d4eead55e393"}}, set()),
    ({"awaiting": {"kind": "ci", "ref": 2208}}, set()),
    ({"awaiting": f"{PR}"}, set()),
    ({"reason": f"waiting on {PR} and {OTHER}"}, {PR, OTHER}),
    ({"reason": ["not", PR]}, set()),
    ({"wake_check": PROBE}, {PR}),
    ({"wake_check": f"x --repo={REPO} --number=2208"}, {PR}),
    ({"wake_check": f"x --number 2208 --repo {REPO}"}, set()),  # only ship's exact order
    ({"wake_check": f"x --repo {REPO} --number 22080x"}, set()),
    ({"wake_check": ["--repo", REPO]}, set()),
    ("not a dict", set()),
    (None, set()),
])
def test_park_named_prs_reads_only_the_park_fields(payload, expected):
    from hermes_cli.kanban_db_dispatch_pr_policy import norm_pr
    assert park_named_prs(payload) == {norm_pr(u) for u in expected}
