"""Only a human operator's canonical renewal replaces the Board's reviewer allowance; everything else holds."""
from contextlib import contextmanager
import json
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_allowance_maintenance as maintenance
from hermes_cli import kanban_allowance_observation as observation
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_release as release
from hermes_cli import kanban_scope_renewal as renewal
from hermes_cli import kanban_spend_caused_review as caused
from hermes_cli import kanban_spend_decision as decision
from hermes_cli import kanban_spend_reviewer as reviewer
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_allowance_observation as reads
from tests.hermes_cli import test_kanban_spend_allowance as fixtures

kanban_home = fixtures.kanban_home
primary = fixtures.primary
HUMAN = release.human_writer("test-operator")
AGENT = ("agent", "20261112_052539_8d8534", "4242:HERMES_SESSION_ID")
PERSON = renewal.Actor("human", None, "human")


@pytest.fixture(autouse=True)
def isolated_cursors():
    maintenance._CURSORS.clear()
    yield
    maintenance._CURSORS.clear()


def _generic(conn, tid, **payload):
    """A later generic budget (a release or ``spend-budget``): cumulative locally, no private authority."""
    return fixtures._override(conn, tid, **{"runs": 25, "cache_read_tokens": 0, "by": "user", **payload})


def _ready(conn, tid):
    with fixtures.kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))


def _decided(primary, monkeypatch, **changes):
    """Reviewer-released card a generic budget was later written over; the Board still reports its allowance."""
    tid, uid = fixtures._released(primary)
    _generic(primary, tid)
    _ready(primary, tid)
    calls = reads._hub(monkeypatch, primary, reads._reply(primary, tid, uid, used=3, remaining=7, **changes))
    return tid, uid, calls


def _stopped(conn, tid):
    """An ordinary pending stop (no scope restriction) and a human mandate written after it."""
    with fixtures.kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        kb._append_event(conn, tid, decision.EVENT, {"reason": "Spend budget: allowance exhausted"})
    stop = decision.pending_episode(conn, tid)
    return renewal.key(stop)


# -- the Board's own state, whatever the local budget says ---------------------------------------------------

@pytest.mark.parametrize("reply, want", [
    ({"allowance": None}, {}), (None, None), ([], None), ({}, None), ({"board": "wrong", "allowance": None}, None),
    (client.HubError(503, "rollover"), None), (TimeoutError("deadline"), None)])
def test_board_state_separates_none_held_from_unknown(primary, monkeypatch, reply, want):
    tid, _ = fixtures._released(primary)
    if isinstance(reply, dict) and "board" not in reply and reply:
        reply = {"board": fixtures.sync.sync_state(primary)["board_id"], **reply}
    reads._hub(monkeypatch, primary, reply)
    assert observation.board_state(primary, tid) == want


def test_board_state_is_unknown_when_no_transport_opens(primary, monkeypatch):
    tid, _ = fixtures._released(primary)

    def refuse(*args, **kwargs):
        raise client.SyncUnavailable("no identity")
    monkeypatch.setattr(client, "load_identity", lambda: SimpleNamespace(gateway="test-home"))
    monkeypatch.setattr(client, "Hub", refuse)
    assert observation.board_state(primary, tid) is None  # nothing to close; never an AttributeError
    assert observation.read(primary, tid) is None


@pytest.mark.parametrize("data", [None, [], "active", 7])
def test_a_non_object_allowance_never_proves_exhaustion(primary, monkeypatch, data):
    tid, _ = fixtures._released(primary)
    reads._hub(monkeypatch, primary, {"board": fixtures.sync.sync_state(primary)["board_id"], "allowance": data})
    assert observation.read(primary, tid) is None


def test_enrich_gives_ordinary_stops_the_canonical_corpus_only_on_a_canonical_board(primary, monkeypatch):
    tid, _ = fixtures._released(primary)
    _stopped(primary, tid)
    stop = decision.pending_episode(primary, tid)
    ctx = caused.enrich(primary, tid, stop, {"comments": [], "runs": []})
    assert ctx["_canonical"] is True and "cause" not in ctx  # ordinary: no cause is invented
    assert ctx["target_evidence"] == ["Spend budget: allowance exhausted"] == ctx["evidence"]
    monkeypatch.setattr(fixtures.sync, "board_mode", lambda conn: "off")
    assert caused.enrich(primary, tid, stop, {"comments": [], "runs": []}) == {"comments": [], "runs": [], "_canonical": False}


@pytest.mark.parametrize("changes", [{"runs_basis": "unknown"}, {"task": "other"}, {"state": "superseded"},
                                     {"state": None}, {"review_uid": ""}, {"review_uid": 7}, {"budget_event_uid": ""}])
def test_board_state_malformed_allowance_is_unknown(primary, monkeypatch, changes):
    tid, uid = fixtures._released(primary)
    reads._hub(monkeypatch, primary, reads._reply(primary, tid, uid, **changes))
    assert observation.board_state(primary, tid) is None


@pytest.mark.parametrize("state", ["active", "invalidated"])
def test_board_state_reports_a_held_allowance_without_local_agreement(primary, monkeypatch, state):
    tid, _, calls = _decided(primary, monkeypatch, state=state)
    seen = observation.board_state(primary, tid)
    assert seen["state"] == state and seen["review_uid"] == fixtures.REVIEW
    assert calls[-1] == ("close",)


def test_board_state_never_reads_in_a_transaction_or_off_primary(primary, monkeypatch):
    tid, _, calls = _decided(primary, monkeypatch)
    with fixtures.kbc.write_txn(primary):
        assert observation.board_state(primary, tid) is None
    fixtures.sync.set_mode(primary, "shadow")
    assert observation.board_state(primary, tid) is None
    assert calls == []


def test_board_state_deadline_is_bounded_and_an_expired_one_reads_nothing(primary, monkeypatch):
    tid, _, calls = _decided(primary, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(observation.time, "monotonic", lambda: clock[0])
    assert observation.board_state(primary, tid, deadline=100.0) is None
    assert calls == []
    assert observation.board_state(primary, tid, deadline=900.0)
    assert calls[0] == ("open", 105.0)
    assert observation.board_state(primary, tid, deadline=102.0)
    assert ("open", 102.0) in calls


def test_board_state_that_answers_after_its_deadline_is_unknown(primary, monkeypatch):
    tid, uid = fixtures._released(primary)
    clock = [100.0]
    monkeypatch.setattr(observation.time, "monotonic", lambda: clock[0])

    class Slow:
        def __init__(self, ident, *, deadline):
            pass

        def reviewer_allowance(self, board, task):
            clock[0] = 105.0
            return reads._reply(primary, tid, uid)

        def close(self):
            pass
    monkeypatch.setattr(client, "load_identity", lambda: SimpleNamespace(gateway="test-home"))
    monkeypatch.setattr(client, "Hub", Slow)
    assert observation.board_state(primary, tid) is None


# -- decided but stuck ---------------------------------------------------------------------------------------

def test_stuck_names_the_held_allowance_only_for_a_generic_newest_budget(primary, monkeypatch):
    tid, _, calls = _decided(primary, monkeypatch)
    held = observation.stuck(primary, tid)
    assert (held.review_uid, held.state) == (fixtures.REVIEW, "active")
    assert observation.still_stuck(primary, tid, held)
    governed, _uid = fixtures._released(primary)  # newest budget is the Board's own: exhaustion's path, not this one
    before = len(calls)
    assert observation.stuck(primary, governed) is None
    plain = kb.create_task(primary, title="never reviewed", assignee="worker")
    _generic(primary, plain)
    assert observation.stuck(primary, plain) is None
    assert len(calls) == before  # neither asked the Board


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_declined_claim_transaction_never_reaches_allowance_or_cas(primary, monkeypatch, lane):
    tid = kb.create_task(primary, title="Declined admission", assignee="worker")
    with fixtures.kbc.write_txn(primary):
        primary.execute("UPDATE tasks SET status=? WHERE id=?", (lane, tid))
    fixtures.sync.set_mode(primary, "off")
    entered = []

    @contextmanager
    def declined(conn, **kwargs):
        entered.append(True)
        yield False

    def forbidden(*args, **kwargs):
        pytest.fail("declined transaction reached allowance check or claim CAS")

    monkeypatch.setattr(kb, "claim_transaction", declined)
    monkeypatch.setattr(kb._spend_allowance, "claim_refused", forbidden)
    monkeypatch.setattr(kb, "claim_and_open_run", forbidden)
    claim = kb.claim_task if lane == "ready" else kb.claim_review_task
    assert claim(primary, tid) is None
    assert entered == [True]
    row = primary.execute("SELECT status,current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()
    assert tuple(row) == (lane, None)
    assert not primary.execute("SELECT 1 FROM task_runs WHERE task_id=?", (tid,)).fetchone()


def test_stuck_preserves_the_callers_shorter_deadline(primary, monkeypatch):
    tid, _, calls = _decided(primary, monkeypatch)
    monkeypatch.setattr(observation.time, "monotonic", lambda: 100.0)
    assert observation.stuck(primary, tid, deadline=100.0) is None
    assert calls == []
    held = observation.stuck(primary, tid, deadline=102.0)
    assert held.deadline == 102.0
    assert calls[0] == ("open", 102.0)


def test_primary_sweep_without_sync_identity_does_not_read_or_pace(primary, monkeypatch):
    tid, _, calls = _decided(primary, monkeypatch)
    monkeypatch.setattr(fixtures.sync, "board_mode", lambda conn: "primary")
    monkeypatch.setattr(fixtures.sync, "sync_state", lambda conn: None)
    assert maintenance.sweep(primary) == []
    assert calls == [] and not maintenance._CURSORS
    assert kb.get_task(primary, tid).status == "ready"


def test_stuck_is_not_claimed_when_the_board_holds_none_or_is_unknown(primary, monkeypatch):
    tid, _uid = fixtures._released(primary)
    _generic(primary, tid)
    board = fixtures.sync.sync_state(primary)["board_id"]
    reads._hub(monkeypatch, primary, {"board": board, "allowance": None})
    assert observation.stuck(primary, tid) is None
    reads._hub(monkeypatch, primary, client.HubError(503, "rollover"))
    assert observation.stuck(primary, tid) is None
    with fixtures.kbc.write_txn(primary):
        assert observation.stuck(primary, tid) is None


def test_still_stuck_is_invalidated_by_any_newer_budget_board_or_expiry(primary, monkeypatch):
    tid, _, _ = _decided(primary, monkeypatch)
    held = observation.stuck(primary, tid)
    assert not observation.still_stuck(primary, tid, None)
    monkeypatch.setattr(observation.time, "monotonic", lambda: held.deadline)
    assert not observation.still_stuck(primary, tid, held)
    monkeypatch.undo()
    reads._hub(monkeypatch, primary, None)
    assert observation.still_stuck(primary, tid, held)
    assert not observation.still_stuck(primary, tid, observation.Held("other", held.budget, "r", "active", held.deadline))
    _generic(primary, tid, runs=30)
    assert not observation.still_stuck(primary, tid, held)
    again = observation.Held(held.board, observation._budget(primary, tid), "r", "active", time.monotonic() + 5)
    assert observation.still_stuck(primary, tid, again)
    fixtures._override(primary, tid, runs=10, runs_basis=fixtures.allowance.BASIS, review_uid="successor")
    assert not observation.still_stuck(primary, tid, observation.Held(
        held.board, observation._budget(primary, tid), "r", "active", time.monotonic() + 5))


@pytest.mark.parametrize("state", ["active", "invalidated"])
def test_sweep_triages_a_decided_but_stuck_card_once_with_the_human_route(primary, monkeypatch, state):
    tid, _, calls = _decided(primary, monkeypatch, state=state)
    monkeypatch.setattr(reviewer, "settings", lambda cfg=None: (False, 2))
    assert maintenance.sweep(primary) == [tid]
    assert sum(call[0] == "read" for call in calls) == 1
    assert kb.get_task(primary, tid).status == "triage"
    stop = decision.pending_episode(primary, tid)
    payload = json.loads(stop["payload"])
    assert payload["allowance"] == {"basis": fixtures.allowance.BASIS, "review_uid": fixtures.REVIEW, "state": state,
                                    "board": fixtures.sync.sync_state(primary)["board_id"], "superseded": False}
    assert "hermes kanban release" in payload["reason"] and state in payload["reason"]
    notice = primary.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, decision.NOTICE)).fetchone()
    assert notice is not None and json.loads(notice[0])["episode"] == renewal.key(stop)
    before = primary.execute("SELECT COUNT(*) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
    maintenance._CURSORS.clear()
    assert maintenance.sweep(primary) == []
    assert primary.execute("SELECT COUNT(*) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0] == before


def test_stuck_triage_refuses_foreign_stale_claimed_and_missing_readings(primary, monkeypatch):
    from hermes_cli import kanban_wake_check
    tid, _, _ = _decided(primary, monkeypatch)
    held = observation.stuck(primary, tid)
    assert not maintenance.triage_stuck(primary, tid, None)
    with fixtures.kbc.write_txn(primary):
        primary.execute("UPDATE tasks SET claim_lock='other',claim_expires=? WHERE id=?", (int(time.time()) + 100, tid))
    assert not maintenance.triage_stuck(primary, tid, held)
    with fixtures.kbc.write_txn(primary):
        primary.execute("UPDATE tasks SET claim_lock=NULL,claim_expires=NULL WHERE id=?", (tid,))
    monkeypatch.setattr(kanban_wake_check, "foreign_copy", lambda conn, task: True)
    assert not maintenance.triage_stuck(primary, tid, held)
    monkeypatch.setattr(kanban_wake_check, "foreign_copy", lambda conn, task: False)
    _generic(primary, tid, runs=40)
    assert not maintenance.triage_stuck(primary, tid, held)
    assert kb.get_task(primary, tid).status == "ready"


def test_a_healthy_or_unknown_card_is_never_triaged_as_stuck(primary, monkeypatch):
    tid, _uid = fixtures._released(primary)
    _generic(primary, tid)
    _ready(primary, tid)
    reads._hub(monkeypatch, primary, {"board": fixtures.sync.sync_state(primary)["board_id"], "allowance": None})
    assert maintenance.sweep(primary) == []
    maintenance._CURSORS.clear()
    reads._hub(monkeypatch, primary, client.HubError(503, "rollover"))
    assert maintenance.sweep(primary) == []
    assert kb.get_task(primary, tid).status == "ready"


# -- release: who may replace it -----------------------------------------------------------------------------

def _state(tid, writer=HUMAN, authority="human"):
    return SimpleNamespace(tid=tid, writer=writer, authority=authority)


def _refusal(conn, s):
    with pytest.raises(release.Refusal) as caught:
        release._board_allowance(conn, s)
    return caught.value.code


def test_release_without_allowance_history_never_asks_the_board(primary, monkeypatch):
    tid = kb.create_task(primary, title="plain", assignee="worker")
    _generic(primary, tid)
    calls = reads._hub(monkeypatch, primary, client.HubError(503, "down"))
    s = _state(tid)
    release._board_allowance(primary, s)
    assert (s.supersede, s.keep_allowance, calls) == (False, False, [])


def test_release_with_an_unreadable_or_invalidated_allowance_is_refused_for_everyone(primary, monkeypatch):
    tid, uid = fixtures._released(primary)
    for writer, authority in ((HUMAN, "human"), (AGENT, "chat")):
        reads._hub(monkeypatch, primary, client.HubError(503, "down"))
        assert _refusal(primary, _state(tid, writer, authority)) == "allowance_unreadable"
        reads._hub(monkeypatch, primary, reads._reply(primary, tid, uid, state="invalidated"))
        assert _refusal(primary, _state(tid, writer, authority)) == "allowance_invalidated"


def test_release_after_the_board_retired_it_is_an_ordinary_release(primary, monkeypatch):
    tid, _ = fixtures._released(primary)
    reads._hub(monkeypatch, primary, {"board": fixtures.sync.sync_state(primary)["board_id"], "allowance": None})
    for writer, authority in ((HUMAN, "human"), (AGENT, "chat")):
        s = _state(tid, writer, authority)
        release._board_allowance(primary, s)
        assert (s.supersede, s.keep_allowance) == (False, False)


def test_only_a_human_operator_supersedes_an_active_allowance(primary, monkeypatch):
    tid, uid = fixtures._released(primary)
    reads._hub(monkeypatch, primary, reads._reply(primary, tid, uid, used=4, remaining=6))
    s = _state(tid)
    release._board_allowance(primary, s)
    assert (s.supersede, s.keep_allowance) == (True, False)
    for writer, authority in ((AGENT, "chat"), (AGENT, "delegate"), (AGENT, "creator_task"), (AGENT, "human"),
                              (HUMAN, "chat")):
        s = _state(tid, writer, authority)
        release._board_allowance(primary, s)
        assert (s.supersede, s.keep_allowance) == (False, True), (writer, authority)


def test_an_agent_release_never_replaces_an_exhausted_or_overwritten_allowance(primary, monkeypatch):
    tid, uid = fixtures._released(primary)
    reads._hub(monkeypatch, primary, reads._reply(primary, tid, uid))  # used 10, remaining 0
    assert _refusal(primary, _state(tid, AGENT, "chat")) == "allowance_exhausted"
    s = _state(tid)
    release._board_allowance(primary, s)
    assert s.supersede  # the human route the refusal names
    _generic(primary, tid)
    reads._hub(monkeypatch, primary, reads._reply(primary, tid, uid, used=4, remaining=6))
    assert _refusal(primary, _state(tid, AGENT, "chat")) == "allowance_in_force"


def test_a_kept_allowance_writes_no_budget(primary, monkeypatch):
    tid, _uid = fixtures._released(primary)
    s = SimpleNamespace(tid=tid, writer=AGENT, runs=5, keep_allowance=True)
    assert release._budget_plan(primary, s)[:2] == (None, None)
    s.keep_allowance = False
    assert release._budget_plan(primary, s)[0] == 5


# -- the renewal a human supersession rides on ---------------------------------------------------------------

def _mandate(conn, tid, kind="human"):
    return kb.add_comment(conn, tid, "default", "Renewed: finish the acceptance.", writer_kind=kind,
                          writer_session=AGENT[1] if kind == "agent" else None)


def _prepare(conn, tid, episode, comment, actor=PERSON, **kw):
    return renewal.prepare(conn, tid, episode=episode, comment_id=comment, reason="operator release", actor=actor, **kw)


def test_allowance_only_renewal_is_a_human_path_that_covers_nothing(primary):
    tid, _ = fixtures._released(primary)
    episode = _stopped(primary, tid)
    comment = _mandate(primary, tid)
    payload = _prepare(primary, tid, episode, comment, allowance_only=True)
    assert payload["covered_restriction_uids"] == [] and payload["authority_kind"] == "human"
    for actor, kw in ((PERSON, {}), (renewal.Actor("agent", AGENT[1], "chat"), {"allowance_only": True}),
                      (renewal.Actor("agent", AGENT[1], "human"), {"allowance_only": True}),
                      (renewal.Actor("human", None, "delegate", ref="g"), {"allowance_only": True})):
        with pytest.raises(renewal.RenewalRefused) as caught:
            _prepare(primary, tid, episode, comment, actor=actor, **kw)
        assert caught.value.code == "not_scope_stopped", (actor, kw)


def test_allowance_only_renewal_keeps_the_mandate_fences(primary):
    tid, _ = fixtures._released(primary)
    early = _mandate(primary, tid)
    episode = _stopped(primary, tid)
    with pytest.raises(renewal.RenewalRefused) as caught:
        _prepare(primary, tid, episode, early, allowance_only=True)
    assert caught.value.code == "mandate_stale"
    with pytest.raises(renewal.RenewalRefused) as caught:
        _prepare(primary, tid, episode, _mandate(primary, tid, "agent"), allowance_only=True)
    assert caught.value.code == "mandate_authority"
    with pytest.raises(renewal.RenewalRefused) as caught:
        _prepare(primary, tid, "another-stop", _mandate(primary, tid), allowance_only=True)
    assert caught.value.code == "episode_mismatch"


# -- advisory cap alignment ----------------------------------------------------------------------------------

def _grant(conn, tid, episode):
    with fixtures.kbc.write_txn(conn):
        kb._append_event(conn, tid, "spend_review", {"version": 1, "verdict": "release", "episode": episode})


def _renewed(conn, tid, **payload):
    with fixtures.kbc.write_txn(conn):
        kb._append_event(conn, tid, renewal.EVENT, {"version": 1, "authority_kind": "human", "actor_kind": "human",
                                                    **payload})


def test_grant_count_restarts_after_a_canonical_human_renewal_only(primary):
    tid = kb.create_task(primary, title="capped", assignee="worker")
    _grant(primary, tid, "stop-1")
    _grant(primary, tid, "stop-2")
    assert caused.grant_count(primary, tid) == 2
    for forged in ({"authority_kind": "chat"}, {"actor_kind": "agent"}, {"version": 2}):
        _renewed(primary, tid, **forged)
        assert caused.grant_count(primary, tid) == 2, forged
    _renewed(primary, tid)
    assert caused.grant_count(primary, tid) == 0
    _grant(primary, tid, "stop-3")
    assert caused.grant_count(primary, tid) == 1
    fixtures.sync.set_mode(primary, "shadow")  # no canonical Board decides here: stay conservative
    assert caused.grant_count(primary, tid) == 3
