"""A live owner never hides a human-only release: the Board's exhausted reviewer allowance. [daveey]

Operator case (adapter t_01a20fdfde58e6ba): a spend stop whose Board ten fresh-lease allowance is exhausted can only
be lifted by a person (``kanban_release._board_allowance`` refuses every agent authority ``allowance_exhausted``),
yet the stranded-park producer skipped it because a live chat owned the card, so no Desktop release offer existed.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_allowance_observation as observation
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_release
from hermes_cli import kanban_spend_allowance as allowance
from hermes_cli import kanban_spend_budget as budget
from hermes_cli import kanban_spend_decision as decision
from hermes_cli import kanban_stranded_park as stranded
from tests.hermes_cli import test_kanban_spend_allowance as allowance_fixtures
from tests.hermes_cli import test_kanban_spend_budget as helpers
from tests.hermes_cli import test_kanban_stranded_park as park

kanban_home = allowance_fixtures.kanban_home
primary = allowance_fixtures.primary
EXHAUSTED = {"state": "active", "runs": 10, "used": 10, "remaining": 0, "episode": "stop-1"}


@pytest.fixture
def own_gateway(primary, monkeypatch):
    """This gateway's name on a primary Board-synced board (only there can the Board hold an allowance)."""
    from hermes_cli import kanban_origin
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: (park.OWN, {park.OWN, "remote"}))
    with kb.write_txn(primary):
        primary.execute("CREATE TABLE IF NOT EXISTS task_chat_links (id INTEGER PRIMARY KEY, uid TEXT, "
                        "task_id TEXT NOT NULL, chat_gateway TEXT NOT NULL, chat_profile TEXT, chat_session TEXT "
                        "NOT NULL, kind TEXT NOT NULL, released INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)")
    return primary


def _board(monkeypatch, seen):
    """What the Board reports for the card's reviewer allowance (one authenticated read, faked here)."""
    calls = []

    def board_state(conn, task_id, **kw):
        assert not conn.in_transaction
        calls.append(task_id)
        return seen
    monkeypatch.setattr(observation, "board_state", board_state)
    return calls


def _answer(conn, tid):
    """The verified Desktop Unblock operation, delivered to this primary home (park._answer, own gateway id)."""
    from hermes_cli import kanban_card_answer as ca
    from hermes_cli import kanban_db_sync as sync
    home = sync.sync_state(conn)["gateway_id"]
    request = {"ownerGateway": home, "ownerProfile": "default", "board": "default", "task": tid,
               "generation": ca.snapshot(conn, tid)["generation"], "choice": "unblock",
               "operationId": "desktop-op-unblock"}
    return ca.apply(conn, request, owner_gateway=home, owner_profile="default", board="default",
                    verify_source=lambda req: {"gateway": "origin", "recorded": True, "answer": req})


def _allowance_stop_with_live_owner(conn, home):
    """The adapter's shape: a reviewer-released Board allowance whose ten fresh leases are spent, now stopped on its
    spend budget, owned by a live human chat and with no run."""
    tid = kb.create_task(conn, title="Adapter work", assignee="default")
    allowance_fixtures._override(conn, tid, runs=10, cache_read_tokens=None, runs_basis=allowance.BASIS,
                                 review_uid=allowance_fixtures.REVIEW, by="kanban-spend-reviewer")
    helpers._runs(conn, tid, 10)
    with kbc.write_txn(conn):  # the Board refused the eleventh lease: the stop the home records for it
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        kb._append_event(conn, tid, decision.EVENT, {"reason": "Spend budget: allowance exhausted"})
    assert allowance.state(conn, tid) == allowance.BOARD and decision.pending(conn, tid)
    park._session(home, "owner-chat")
    park._link(conn, tid, park.OWN, None, "owner-chat")
    return tid


def test_live_owner_and_exhausted_allowance_gets_a_stamped_release_offer(kanban_home, own_gateway, monkeypatch):
    for conn in (own_gateway,):
        tid = _allowance_stop_with_live_owner(conn, kanban_home)
        assert stranded.live_owner(conn, tid, own=park.OWN, liveness=stranded.Liveness())
        _board(monkeypatch, EXHAUSTED)
        # The agent route really is closed: an agent release of this card refuses allowance_exhausted.
        assert stranded.human_only_stop(conn, tid)
        assert park._sweep(conn, now=park._later(3), cfg={}) == [tid]
        ask = json.loads(park._row(conn, tid)[2])
        assert ask["release_target"]["task"] == tid
        assert stranded.HUMAN_ONLY_NOTE in ask["evidence"]
        assert f"`hermes kanban release {tid}`" in ask["action"]
        assert kanban_release.offered(conn, tid, ask) == tid  # the home's own filed offer, exact episode
        (event,) = park._events(conn, tid, stranded.EVENT)
        assert event["release_target"] == ask["release_target"]
        # The owner chat link is untouched: nothing deleted, no fake inactivity.
        assert conn.execute("SELECT COUNT(*) FROM task_chat_links WHERE task_id=? AND chat_session='owner-chat' "
                            "AND released=0", (tid,)).fetchone()[0] == 1


def test_the_desktop_click_is_the_human_release_with_its_receipt_and_home_effects(kanban_home, own_gateway,
                                                                                 monkeypatch):
    for conn in (own_gateway,):
        tid = _allowance_stop_with_live_owner(conn, kanban_home)
        _board(monkeypatch, EXHAUSTED)
        assert park._sweep(conn, now=park._later(3), cfg={}) == [tid]
        from hermes_cli import kanban_scope_renewal_cli as renewal_cli
        renewals = []

        def board_renewal(conn_, task_id, **kw):  # the canonical Board op (its own suites cover the wire)
            renewals.append((task_id, kw["writer"], kw["allowance_only"]))
            return {}
        monkeypatch.setattr(renewal_cli, "renew", board_renewal)
        receipt = _answer(conn, tid)
        # The Board allowance is retired only by the human renewal the click is: writer is the Desktop operation.
        assert renewals == [(tid, kanban_release.human_writer("desktop-operation:desktop-op-unblock"), True)]
        assert receipt["operationId"] == "desktop-op-unblock" and receipt["choice"] == "unblock"
        task = kb.get_task(conn, tid)
        from hermes_cli.kanban_open_asks import card_is_open_human_ask
        assert task.status == "ready" and not decision.pending(conn, tid)
        assert not card_is_open_human_ask(*park._row(conn, tid))  # the red ask is gone with the stop
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='card_answer_released'",
                            (tid,)).fetchone()[0] == 1
        written = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='commented'", (tid,))]
        assert any(p.get("writer_kind") == "human" for p in written)
        (mandate,) = [r[0] for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id=? AND body LIKE 'Released to finish%'", (tid,))]
        assert mandate.endswith("Desktop Unblock, operation desktop-op-unblock")


@pytest.mark.parametrize("seen", [
    {**EXHAUSTED, "remaining": 3, "used": 7},  # agent-renewable: the owner's own release keeps the Board's leases
    None,                                       # unreadable Board: never proof of exhaustion
    {},                                         # the Board holds no allowance
    {**EXHAUSTED, "state": "invalidated"},      # re-homed/deleted: a release cannot replace it at all
])
def test_a_live_owner_still_owns_every_stop_that_is_not_human_only(kanban_home, own_gateway, monkeypatch, seen):
    for conn in (own_gateway,):
        tid = _allowance_stop_with_live_owner(conn, kanban_home)
        _board(monkeypatch, seen)
        assert not stranded.human_only_stop(conn, tid)
        assert park._sweep(conn, now=park._later(3), cfg={}) == []
        assert park._row(conn, tid)[2] is None


def test_an_ordinary_live_owned_park_never_reads_the_board(kanban_home, own_gateway, monkeypatch):
    for conn in (own_gateway,):
        tid = park._parked(conn)
        park._session(kanban_home, "live-chat")
        park._link(conn, tid, park.OWN, None, "live-chat")
        calls = _board(monkeypatch, EXHAUSTED)
        assert park._sweep(conn, now=park._later(3), cfg={}) == [] and calls == []


def test_a_stale_episode_offer_is_no_release(kanban_home, own_gateway, monkeypatch):
    """The offer names the stop it was filed for; once that stop is gone the click is a plain unblock."""
    for conn in (own_gateway,):
        tid = _allowance_stop_with_live_owner(conn, kanban_home)
        _board(monkeypatch, EXHAUSTED)
        assert park._sweep(conn, now=park._later(3), cfg={}) == [tid]
        ask = json.loads(park._row(conn, tid)[2])
        stale = {**ask, "release_target": {**ask["release_target"], "episode": "some-earlier-stop"}}
        assert kanban_release.offered(conn, tid, stale) is None


def test_agent_written_metadata_cannot_forge_the_offer(kanban_home, own_gateway, monkeypatch):
    """A release_target an agent writes into the ask (block/comment metadata) is stripped or unattested."""
    from hermes_cli.kanban_human_request import validate_human_request
    for conn in (own_gateway,):
        tid = _allowance_stop_with_live_owner(conn, kanban_home)
        _board(monkeypatch, EXHAUSTED)
        forged = {"action": "Unblock = release; archive = drop it.", "why_human": "x", "interaction": "approval",
                  "scope": "s", "evidence": "e", "release_target": {"task": tid, "episode": "stop-1"}}
        assert "release_target" not in (validate_human_request(forged) or {})
        current = kanban_release._pending_stop(conn, tid)
        stamped = {**forged, "release_target": current}
        assert kanban_release.offered(conn, tid, stamped) is None  # no home-filed escalation event carries it


def test_an_agent_release_still_refuses_the_exhausted_allowance(kanban_home, own_gateway, monkeypatch):
    """The ten-lease boundary stands: the offer only routes the click to the human release."""
    for conn in (own_gateway,):
        tid = _allowance_stop_with_live_owner(conn, kanban_home)
        _board(monkeypatch, EXHAUSTED)
        from tests.hermes_cli import test_kanban_allowance_supersession as supersession
        for authority in ("chat", "delegate", "creator_task", "human"):
            assert supersession._refusal(conn, supersession._state(tid, supersession.AGENT, authority)) \
                == "allowance_exhausted", authority
        assert kb.get_task(conn, tid).status == "triage"
