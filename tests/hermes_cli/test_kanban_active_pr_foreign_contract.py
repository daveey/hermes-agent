"""active_pr must not bind a card to another card's pull, or to a non-blocking mention.

A comment that only names another live card's completion_contract pull is not this
card's published work. Neither is a comment whose only purpose is to say that pull
does not block this card. This card's own pull still holds. Only this card's own
(or unknown-provenance) comments are publication candidates; known foreign-writer
comments are FYI before this policy runs. The hold notice must read checks with the
same gh argv as pr_state, so a broker-readable pull with zero failing checks is not
reported as unavailable. [daveey]
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_guard_notice as notice
from hermes_cli import kanban_db_dispatch_pr_state as prs
from tests.hermes_cli.kanban_guard_helpers import worker_comment
from tests.hermes_cli.test_kanban_park_invariants import kanban_home

__all__ = ["kanban_home"]
pytestmark = pytest.mark.usefixtures("kanban_home")

FOREIGN = "https://github.com/daveey/hermes/pull/210"
OWN = "https://github.com/daveey/hermes/pull/19"
OTHER = "https://github.com/daveey/hermes/pull/77"
BROKER = ["token-broker", "exec", "--scope", "user/github", "--", "/usr/bin/gh"]


@pytest.fixture(autouse=True)
def open_pr(monkeypatch):
    states: dict[str, str] = {}
    seen: list[str] = []

    def _state(url):
        seen.append(url)
        return states.get(url, "OPEN")

    monkeypatch.setattr(prs, "pr_state", _state)
    return states, seen


def _claimed(conn, title, **kwargs):
    tid = kb.create_task(conn, title=title, assignee="a", **kwargs)
    kb.claim_task(conn, tid)
    return tid


def _guard(conn, tid):
    return kbd.check_respawn_guard(conn, tid)


def _foreign_writer_comment(conn, tid, body):
    """A comment written from inside ANOTHER card's run: known foreign provenance (FYI)."""
    other = _claimed(conn, "sibling")
    run = conn.execute("SELECT id FROM task_runs WHERE task_id=?", (other,)).fetchone()[0]
    with pytest.MonkeyPatch.context() as env:
        env.setenv("HERMES_KANBAN_TASK", other)
        env.setenv("HERMES_KANBAN_RUN_ID", str(run))
        return kb.add_comment(conn, tid, "a", body)


def _drop_provenance(conn, tid, cid):
    """Delete the comment's ``commented`` event: provenance unknown, which fails closed."""
    conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='commented' "
                 "AND json_extract(payload, '$.comment_id')=?", (tid, cid))
    conn.commit()


class _Proc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_own_comment_that_only_names_another_live_cards_contract_does_not_hold():
    with kbc.connect() as conn:
        kb.create_task(conn, title="land the other pull", assignee="b", completion_contract=FOREIGN)
        tid = _claimed(conn, "not that pull")
        worker_comment(conn, tid, "a", f"See {FOREIGN}")
        assert _guard(conn, tid) is None


def test_own_nonblocking_comment_alone_does_not_hold():
    with kbc.connect() as conn:
        tid = _claimed(conn, "informational mention")
        worker_comment(conn, tid, "a", f"{FOREIGN} only changes wording. It doesn't block you.")
        assert _guard(conn, tid) is None


def test_foreign_contract_match_ignores_case_and_trailing_slash():
    with kbc.connect() as conn:
        owner = kb.create_task(conn, title="land", assignee="b", completion_contract=FOREIGN)
        conn.execute("UPDATE tasks SET completion_contract = ? WHERE id = ?", (FOREIGN.upper() + "/", owner))
        conn.commit()
        tid = _claimed(conn, "not that pull")
        worker_comment(conn, tid, "a", f"Named only {FOREIGN}")
        assert _guard(conn, tid) is None


def test_own_pull_url_still_holds():
    with kbc.connect() as conn:
        tid = _claimed(conn, "opened our pull")
        worker_comment(conn, tid, "a", f"Opened {OWN}")
        assert _guard(conn, tid) == "active_pr"


def test_own_contract_holds_even_when_comment_says_it_does_not_block():
    with kbc.connect() as conn:
        tid = _claimed(conn, "our published pull", completion_contract=OWN)
        worker_comment(conn, tid, "a", f"{OWN} does not block this card.")
        assert _guard(conn, tid) == "active_pr"


def test_done_cards_contract_does_not_exempt():
    with kbc.connect() as conn:
        owner = kb.create_task(conn, title="already landed", assignee="b", completion_contract=FOREIGN)
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (owner,))
        conn.commit()
        tid = _claimed(conn, "mentioned a finished pull")
        worker_comment(conn, tid, "a", f"Opened {FOREIGN}")
        assert _guard(conn, tid) == "active_pr"


def test_foreign_url_does_not_drop_an_own_url_in_the_same_comment():
    with kbc.connect() as conn:
        kb.create_task(conn, title="land", assignee="b", completion_contract=FOREIGN)
        tid = _claimed(conn, "opened our pull too")
        worker_comment(conn, tid, "a", f"See {FOREIGN}. Opened {OWN}")
        assert _guard(conn, tid) == "active_pr"


def test_nonblocking_comment_does_not_exempt_a_real_open_comment():
    with kbc.connect() as conn:
        tid = _claimed(conn, "opened then disclaimed")
        worker_comment(conn, tid, "a", f"{OWN} does not block this card.")
        worker_comment(conn, tid, "a", f"Opened {OWN}")
        assert _guard(conn, tid) == "active_pr"


def test_unknown_provenance_naming_another_pull_still_holds():
    """Fail closed: a comment with no readable provenance is still a candidate."""
    with kbc.connect() as conn:
        tid = _claimed(conn, "unknown writer")
        cid = worker_comment(conn, tid, "a", f"Opened {OTHER}")
        _drop_provenance(conn, tid, cid)
        assert prs._foreign_pr_comment_ids(conn, tid) == set()
        assert _guard(conn, tid) == "active_pr"


def test_unknown_provenance_naming_only_a_foreign_contract_does_not_hold():
    with kbc.connect() as conn:
        kb.create_task(conn, title="land", assignee="b", completion_contract=FOREIGN)
        tid = _claimed(conn, "unknown writer")
        cid = worker_comment(conn, tid, "a", f"See {FOREIGN}")
        _drop_provenance(conn, tid, cid)
        assert _guard(conn, tid) is None


def test_fyi_mention_does_not_undo_an_own_disclaimer():
    """A foreign-writer comment naming the same pull is FYI, not a second candidate."""
    with kbc.connect() as conn:
        tid = _claimed(conn, "disclaimed")
        worker_comment(conn, tid, "a", f"{OTHER} won't block this card.")
        _foreign_writer_comment(conn, tid, f"Opened {OTHER}")
        assert _guard(conn, tid) is None


def test_mixed_own_foreign_contract_nonblocking_and_fyi_only_own_pull_is_consulted(open_pr):
    states, seen = open_pr
    with kbc.connect() as conn:
        kb.create_task(conn, title="land", assignee="b", completion_contract=FOREIGN)
        tid = _claimed(conn, "mixed")
        worker_comment(conn, tid, "a", f"Opened {OWN}. See {FOREIGN}.")
        worker_comment(conn, tid, "a", f"{OTHER} does not block this card.")
        _foreign_writer_comment(conn, tid, "Sibling opened https://github.com/daveey/hermes/pull/88")
        assert _guard(conn, tid) == "active_pr"
        assert set(seen) == {OWN}
        # Once our only holding pull merges, the open foreign/disclaimed pulls keep nothing held.
        states[OWN] = "MERGED"
        seen.clear()
        assert _guard(conn, tid) is None
        assert set(seen) == {OWN}


def test_filtered_comment_does_not_advance_latest_pr_comment_id(monkeypatch):
    """Only comments that keep a holding pull order the immediate-checkpoint watermark."""
    captured = {}

    def _checkpoint(conn, task_id, since, latest_pr_comment_id):
        captured["latest"] = latest_pr_comment_id
        return False

    monkeypatch.setattr(prs, "_immediate_checkpoint_after", _checkpoint)
    with kbc.connect() as conn:
        kb.create_task(conn, title="land", assignee="b", completion_contract=FOREIGN)
        tid = _claimed(conn, "watermark")
        own = worker_comment(conn, tid, "a", f"Opened {OWN}")
        worker_comment(conn, tid, "a", f"See {FOREIGN}")
        worker_comment(conn, tid, "a", f"{OTHER} doesn't block this task.")
        assert _guard(conn, tid) == "active_pr"
        assert captured["latest"] == own


def _hold_ready_for_notice(conn, monkeypatch, pr):
    tid = kb.create_task(conn, title="held", assignee="a")
    kb.add_comment(conn, tid, "a", f"Opened {pr}")
    kb._append_event(conn, tid, "respawn_guarded", {"reason": "active_pr"})
    monkeypatch.setenv("HERMES_KANBAN_GUARD_HOLD_NOTIFY_SECONDS", "0")
    return tid


def test_notice_for_broker_readable_zero_failing_does_not_say_unavailable(monkeypatch):
    calls = []

    def _run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return _Proc(8, "lint\tpass\t1s\thttps://example/lint\nunit\tpending\t0\thttps://example/unit\n")

    monkeypatch.setattr(prs, "_gh_argv", lambda: list(BROKER))
    monkeypatch.setattr(notice.subprocess, "run", _run)
    with kbc.connect() as conn:
        tid = _hold_ready_for_notice(conn, monkeypatch, OWN)
        assert notice.notify_guard_hold(conn, tid, "active_pr") is True
        notes = [c for c in kb.list_comments(conn, tid) if c.body.startswith("[respawn-guard]")]
        assert len(notes) == 1
        assert "failing checks unavailable" not in notes[0].body
        assert OWN in notes[0].body
        assert "0 failing checks" in notes[0].body
    assert calls, "notice did not read checks"
    assert calls[0][:len(BROKER)] == BROKER
    assert calls[0][len(BROKER):] == ["pr", "checks", OWN]


def test_notice_reports_failing_checks_through_broker(monkeypatch):
    monkeypatch.setattr(prs, "_gh_argv", lambda: list(BROKER))
    monkeypatch.setattr(notice.subprocess, "run",
                        lambda *a, **k: _Proc(1, "lint\tfail\t1s\thttps://example/lint\n"))
    with kbc.connect() as conn:
        tid = _hold_ready_for_notice(conn, monkeypatch, OWN)
        assert notice.notify_guard_hold(conn, tid, "active_pr") is True
        notes = [c for c in kb.list_comments(conn, tid) if c.body.startswith("[respawn-guard]")]
        assert notes and "1 failing check(s): lint" in notes[0].body


def test_notice_unreadable_checks_still_say_unavailable(monkeypatch):
    monkeypatch.setattr(prs, "_gh_argv", lambda: list(BROKER))
    monkeypatch.setattr(notice.subprocess, "run", lambda *a, **k: _Proc(1, "", "gh: auth required"))
    with kbc.connect() as conn:
        tid = _hold_ready_for_notice(conn, monkeypatch, OWN)
        assert notice.notify_guard_hold(conn, tid, "active_pr") is True
        notes = [c for c in kb.list_comments(conn, tid) if c.body.startswith("[respawn-guard]")]
        assert notes and "failing checks unavailable" in notes[0].body


def test_notice_gh_launch_failure_says_unavailable(monkeypatch):
    def _boom(*a, **k):
        raise OSError("no gh")

    monkeypatch.setattr(prs, "_gh_argv", lambda: list(BROKER))
    monkeypatch.setattr(notice.subprocess, "run", _boom)
    with kbc.connect() as conn:
        tid = _hold_ready_for_notice(conn, monkeypatch, OWN)
        assert notice.notify_guard_hold(conn, tid, "active_pr") is True
        notes = [c for c in kb.list_comments(conn, tid) if c.body.startswith("[respawn-guard]")]
        assert notes and "failing checks unavailable" in notes[0].body


def test_fix_card_does_not_tell_worker_to_unblock_or_schedule(monkeypatch):
    monkeypatch.setattr(prs, "_gh_argv", lambda: list(BROKER))
    monkeypatch.setattr(notice.subprocess, "run", lambda *a, **k: _Proc(0, "lint\tpass\t1s\thttps://example/lint\n"))
    with kbc.connect() as conn:
        tid = _hold_ready_for_notice(conn, monkeypatch, OWN)
        assert notice.notify_guard_hold(conn, tid, "active_pr") is True
        row = conn.execute("SELECT body FROM tasks WHERE idempotency_key = ?", (f"guard-hold:{tid}",)).fetchone()
        assert row is not None
        body = row["body"].lower()
        assert "and unblock it" not in body
        assert "or unblock" not in body
        assert "do not call unblock or schedule" in body
        assert "dispatcher releases the hold" in body
