"""A park wakes when its producer card finishes, even if its own wake_check cannot see that card. [daveey]

MacBook t_01a17ec75ca4f9f6 parked on 10-30 with ``awaiting={kind:'card', ref:'t_01a18cb13e96c856', gateway:'hub'}``
and its own probe ``test -n "$(hermes kanban show t_01a18cb13e96c856 2>/dev/null | grep -i 'status.*done')"``.
``hermes kanban show`` reads only this host's board, where the hub card does not exist, so the probe exited a clean
1 ("not yet") at every check after the hub card completed. The card would have slept 18 h until its timer.

The wake-check runner now re-checks the declared producer card whenever the probe says "not yet":
- remote (gateway set): ``kanban_wait_check.check_remote``, at most once per 15 min per (gateway, ref);
  rc 3 (gateway offline) is unknown and never wakes;
- local: the ref's status on this board;
- ``done``/``archived`` wakes the card once (``wake_probe_stale``, reason ``producer_finished``) with a
  ``kanban-dispatcher`` comment; ``running``/``review``/``blocked`` keep it parked.
"""
from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wait_check as kwait
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli import test_kanban_awaiting as _awaiting_tests
from tests.hermes_cli.test_kanban_awaiting import _as_worker, _running, _tool

kanban_home = _awaiting_tests.kanban_home  # the shared fixture (HERMES_HOME, board, dry run off)

REMOTE = "t_01a18cb13e96c856"


@pytest.fixture(autouse=True)
def _fresh_producer_cache():
    cache = getattr(kwc, "_PRODUCER_CHECKED", {})
    cache.clear()
    yield
    cache.clear()


class FakeRemote:
    """``check_remote`` stand-in: the remote card's status (or an rc-3 offline answer), and a call count."""

    def __init__(self, status: str = "running", rc: int | None = None):
        self.status, self.rc, self.calls = status, rc, []

    def __call__(self, gateway, kind, target, *, board=None, until=kwait.DEFAULT_UNTIL, since=()):
        self.calls.append((gateway, kind, target, board))
        if self.rc is not None:
            return self.rc, f"{target} running (stale replica: hub offline 3600s)"
        if self.status in until:
            return 0, f"{target} {self.status}"
        return 1, f"{target} {self.status}"


def _park_remote(monkeypatch, conn, fake: FakeRemote) -> str:
    monkeypatch.setattr(kwait, "check_remote", fake)
    tid = _running(conn)
    _as_worker(monkeypatch, conn, tid)
    out = _tool("schedule", {"reason": f"waiting for the hub study {REMOTE}", "wake_check": "exit 1",
                             "check_every": 300, "awaiting": {"kind": "card", "ref": REMOTE, "gateway": "hub"}})
    assert out.get("status") == "scheduled", out
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    fake.calls.clear()  # the park-time confirmation is not a runner call
    return tid


def _stale_events(conn, tid):
    return [e for e in kb.list_events(conn, tid) if e.kind == "wake_probe_stale"]


def _assert_woken(conn, tid, ref, gateway_word):
    assert kb.get_task(conn, tid).status == "ready"
    ev = _stale_events(conn, tid)
    assert len(ev) == 1, ev
    p = ev[0].payload
    assert p["reason"] == "producer_finished" and p["ref"] == ref and p["producer_status"] == "done", p
    assert "completed_at" in p
    note = [c for c in kb.list_comments(conn, tid) if c.author == "kanban-dispatcher"][-1].body
    assert f"Woken: producer card {ref} on {gateway_word} is done" in note, note
    assert "hermes kanban show` reads only this host's board" in note, note
    if gateway_word == "this board":
        # Same-host producer: the probe reads this board, so the note must not send the waiter to a --gateway.
        assert f"hermes kanban wait-check task {ref}`" in note, note
        assert "--gateway" not in note, note
    else:
        assert f"hermes kanban wait-check task {ref} --gateway" in note, note
    assert "add `--from <that status>`. Those statuses count as met" in note, note
    ctx = kwc.render_wake_context(conn, tid)
    assert "producer card" in ctx and ref in ctx, ctx


def test_remote_done_producer_wakes_a_park_whose_probe_cannot_see_it(kanban_home, monkeypatch) -> None:
    fake = FakeRemote("running")
    with kbc.connect_closing() as conn:
        tid = _park_remote(monkeypatch, conn, fake)
        fake.status = "done"
        kwc.run_due_wake_checks(conn, now=int(time.time()) + 600, wait=True)
        assert fake.calls == [("hub", "task", REMOTE, None)]
        _assert_woken(conn, tid, REMOTE, "hub")
        assert _stale_events(conn, tid)[0].payload["gateway"] == "hub"


@pytest.mark.parametrize("status,rc", [("running", None), ("review", None), ("blocked", None), ("done", 3)])
def test_remote_producer_not_finished_or_unknown_keeps_the_park(kanban_home, monkeypatch, status, rc) -> None:
    fake = FakeRemote("running")
    with kbc.connect_closing() as conn:
        tid = _park_remote(monkeypatch, conn, fake)
        fake.status, fake.rc = status, rc
        kwc.run_due_wake_checks(conn, now=int(time.time()) + 600, wait=True)
        assert len(fake.calls) == 1
        assert kb.get_task(conn, tid).status == "scheduled"
        assert not _stale_events(conn, tid)
        assert kwc.get_state(conn, tid)["last_rc"] == 1


def test_remote_producer_is_checked_at_most_once_per_15_minutes(kanban_home, monkeypatch) -> None:
    fake = FakeRemote("running")
    with kbc.connect_closing() as conn:
        tid = _park_remote(monkeypatch, conn, fake)
        t0 = int(time.time())
        kwc.run_due_wake_checks(conn, now=t0 + 600, wait=True)
        kwc.run_due_wake_checks(conn, now=t0 + 1200, wait=True)  # the probe is due again (check_every 300)
        assert kwc.get_state(conn, tid)["last_at"] == t0 + 1200  # the second pass really ran the probe
        assert len(fake.calls) == 1, fake.calls
        assert kb.get_task(conn, tid).status == "scheduled"


def test_local_producer_completed_after_the_park_wakes_it(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        producer = _running(conn)
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": f"waiting for {producer}", "wake_check": "exit 1", "check_every": 300,
                                 "awaiting": {"kind": "card", "ref": producer}})
        assert out.get("status") == "scheduled", out
        monkeypatch.delenv("HERMES_KANBAN_TASK")
        kwc.run_due_wake_checks(conn, now=int(time.time()) + 600, wait=True)
        assert kb.get_task(conn, tid).status == "scheduled"  # producer still running
        assert kb.complete_task(conn, producer, summary="study finished")
        kwc.run_due_wake_checks(conn, now=int(time.time()) + 1200, wait=True)
        _assert_woken(conn, tid, producer, "this board")
        assert _stale_events(conn, tid)[0].payload["gateway"] is None
        assert _stale_events(conn, tid)[0].payload["completed_at"].startswith("20")


def test_wake_check_description_says_show_reads_only_this_board() -> None:
    from tools import kanban_tools_schemas as kts

    desc = kts._WAKE_CHECK_PROPS["wake_check"]["description"]
    assert "`hermes kanban show`/`list` read only this host's board." in desc
    assert "use `hermes kanban wait-check task <id> --gateway <gw>`, or omit wake_check" in desc
    # The met set includes blocked/review/triage: an unchanged one needs --from (t_01a17ec75ca4f9f6 woke in 8 s).
    assert "If that card is already blocked, in review or in triage, add `--from <that status>`." in desc
    assert "without it the probe passes at once" in desc


def test_a_wait_check_probe_on_the_same_card_is_not_second_guessed(kanban_home, monkeypatch) -> None:
    """Root t_01a17ec75ca4f9f6, 10-30 14:02 UTC: its producer sat blocked on a spend decision, so the default
    wait-check (met set includes blocked) woke it in 8 s; the operator re-parked with ``--from blocked``, which exits
    1 while the card stays blocked. The producer re-check must leave that probe alone (no extra ssh, no wake)."""
    fake = FakeRemote("blocked")
    with kbc.connect_closing() as conn:
        tid = _park_remote(monkeypatch, conn, fake)
        probe = f"hermes kanban wait-check task {REMOTE} --gateway hub --from blocked"
        assert kwc.finished_producer(conn, tid, probe) is None
        fake.status = "done"  # even done: that probe reads the card itself and exits 0 on its own
        assert kwc.finished_producer(conn, tid, probe) is None
        assert fake.calls == []
