"""active_pr must not bind a card to another card's pull, or to a non-blocking mention.

A comment that only names another live card's completion_contract pull is not this
card's published work. Neither is a comment whose only purpose is to say that pull
does not block this card. This card's own pull still holds. The hold notice must
read checks with the same gh argv as pr_state, so a broker-readable pull with zero
failing checks is not reported as unavailable. [daveey]
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_guard_notice as notice
from hermes_cli import kanban_db_dispatch_pr_state as prs
from tests.hermes_cli.test_kanban_park_invariants import kanban_home

__all__ = ["kanban_home"]
pytestmark = pytest.mark.usefixtures("kanban_home")

FOREIGN = "https://github.com/daveey/hermes/pull/210"
OWN = "https://github.com/daveey/hermes/pull/19"
BROKER = ["token-broker", "exec", "--scope", "user/github", "--", "/usr/bin/gh"]


@pytest.fixture(autouse=True)
def open_pr(monkeypatch):
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")


def _claimed(conn, title, **kwargs):
    tid = kb.create_task(conn, title=title, assignee="a", **kwargs)
    kb.claim_task(conn, tid)
    return tid


def _guard(conn, tid):
    return kbd.check_respawn_guard(conn, tid)


class _Proc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_comment_that_only_names_another_live_cards_contract_does_not_hold():
    with kbc.connect() as conn:
        kb.create_task(conn, title="land the other pull", assignee="b", completion_contract=FOREIGN)
        tid = _claimed(conn, "not that pull")
        kb.add_comment(conn, tid, "parent", f"See {FOREIGN}")
        assert _guard(conn, tid) is None


def test_nonblocking_comment_alone_does_not_hold():
    with kbc.connect() as conn:
        tid = _claimed(conn, "informational mention")
        kb.add_comment(
            conn, tid, "parent",
            f"{FOREIGN} only changes wording. It doesn't block you.",
        )
        assert _guard(conn, tid) is None


def test_foreign_contract_match_ignores_case_and_trailing_slash():
    with kbc.connect() as conn:
        owner = kb.create_task(conn, title="land", assignee="b", completion_contract=FOREIGN)
        conn.execute(
            "UPDATE tasks SET completion_contract = ? WHERE id = ?",
            (FOREIGN.upper() + "/", owner),
        )
        conn.commit()
        tid = _claimed(conn, "not that pull")
        kb.add_comment(conn, tid, "parent", f"Named only {FOREIGN}")
        assert _guard(conn, tid) is None


def test_own_pull_url_still_holds():
    with kbc.connect() as conn:
        tid = _claimed(conn, "opened our pull")
        kb.add_comment(conn, tid, "a", f"Opened {OWN}")
        assert _guard(conn, tid) == "active_pr"


def test_own_contract_holds_even_when_comment_says_it_does_not_block():
    with kbc.connect() as conn:
        tid = _claimed(conn, "our published pull", completion_contract=OWN)
        kb.add_comment(conn, tid, "a", f"{OWN} does not block this card.")
        assert _guard(conn, tid) == "active_pr"


def test_done_cards_contract_does_not_exempt():
    with kbc.connect() as conn:
        owner = kb.create_task(conn, title="already landed", assignee="b", completion_contract=FOREIGN)
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (owner,))
        conn.commit()
        tid = _claimed(conn, "mentioned a finished pull")
        kb.add_comment(conn, tid, "a", f"Opened {FOREIGN}")
        assert _guard(conn, tid) == "active_pr"


def test_foreign_url_does_not_drop_an_own_url_in_the_same_comment():
    with kbc.connect() as conn:
        kb.create_task(conn, title="land", assignee="b", completion_contract=FOREIGN)
        tid = _claimed(conn, "opened our pull too")
        kb.add_comment(conn, tid, "a", f"See {FOREIGN}. Opened {OWN}")
        assert _guard(conn, tid) == "active_pr"


def test_nonblocking_comment_does_not_exempt_a_real_open_comment():
    with kbc.connect() as conn:
        tid = _claimed(conn, "opened then disclaimed")
        kb.add_comment(conn, tid, "a", f"{OWN} does not block this card.")
        kb.add_comment(conn, tid, "a", f"Opened {OWN}")
        assert _guard(conn, tid) == "active_pr"


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
        return _Proc(
            8,
            "lint\tpass\t1s\thttps://example/lint\nunit\tpending\t0\thttps://example/unit\n",
        )

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


def test_notice_unreadable_checks_still_say_unavailable(monkeypatch):
    def _run(cmd, *args, **kwargs):
        return _Proc(1, "", "gh: auth required")

    monkeypatch.setattr(prs, "_gh_argv", lambda: list(BROKER))
    monkeypatch.setattr(notice.subprocess, "run", _run)
    with kbc.connect() as conn:
        tid = _hold_ready_for_notice(conn, monkeypatch, OWN)
        assert notice.notify_guard_hold(conn, tid, "active_pr") is True
        notes = [c for c in kb.list_comments(conn, tid) if c.body.startswith("[respawn-guard]")]
        assert notes and "failing checks unavailable" in notes[0].body


def test_fix_card_does_not_tell_worker_to_unblock_or_schedule(monkeypatch):
    monkeypatch.setattr(prs, "_gh_argv", lambda: list(BROKER))
    monkeypatch.setattr(
        notice.subprocess, "run",
        lambda *a, **k: _Proc(0, "lint\tpass\t1s\thttps://example/lint\n"),
    )
    with kbc.connect() as conn:
        tid = _hold_ready_for_notice(conn, monkeypatch, OWN)
        assert notice.notify_guard_hold(conn, tid, "active_pr") is True
        row = conn.execute(
            "SELECT body FROM tasks WHERE idempotency_key = ?",
            (f"guard-hold:{tid}",),
        ).fetchone()
        assert row is not None
        body = row["body"].lower()
        assert "and unblock it" not in body
        assert "or unblock" not in body
        assert "do not call unblock or schedule" in body
        assert "dispatcher releases the hold" in body
