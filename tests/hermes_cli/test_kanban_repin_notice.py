"""A worker-first card no pool worker can run (``no_eligible_worker``) reaches its owner. [daveey]

Ruling 9 of the approved overflow plan keeps the card queued and fenced (no automatic repin); these tests pin
that the origin then posts one comment + one origin-chat notice per card revision with the exact repin command,
that the stuck summary names the card instead of counting it as a stall, and that nothing repins it.
"""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kn
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_queue_reason as qr
from hermes_cli import kanban_repin_notice as rn
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_worker_placement as wp
from hermes_cli.kanban_db_dispatch_preclaim import eligible_rows
from hermes_cli.kanban_db_dispatch_stuck import describe_suppression, stuck_warning
from tests.hermes_cli import test_kanban_worker_reserve_client as fixtures

conn = fixtures.conn
CFG = {"worker_pools": {"hub": ["worker-1"]}}
COMMAND = 'hermes kanban move {tid} --runs-on machine:hub --reason "no pool worker can run it"'


def _queued(missing=("skill:five-whys",)):
    candidate = {"machine": "worker-1", "reasons": ["missing_labels"]}
    if missing:
        candidate["missing_labels"] = list(missing)
    return {"state": "queued", "machine": None, "reservation_id": None, "expires_at": None,
            "reason": "waiting_for_worker_capacity",
            "observation": {"version": 1, "code": "no_eligible_worker", "observed_at_ms": 1000,
                            "candidates": [candidate]}}


def _hub(reply=None):
    hub = Mock()
    hub.reserve.return_value = reply or _queued()
    return hub


def _card(conn):
    return fixtures.acknowledged(conn, skills=json.dumps(["five-whys"]),
                                 requires=json.dumps(["terminal", "file"]))


def _notices(conn, tid):
    return [json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, rn.NOTICE_KIND))]


def _notice_comments(conn, tid):
    return [c for c in kb.list_comments(conn, tid) if c.body.startswith("No pool worker can run this card")]


def _subscribe(conn, tid, chat="origin-chat"):
    kn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=chat,
                      delivery_metadata={"reason": "create"})


def _delivered(conn, tid, chat="origin-chat"):
    from gateway.kanban_watchers_notifier import TERMINAL_KINDS
    _old, _new, events = kn.claim_unseen_events_for_sub(conn, task_id=tid, platform="tui", chat_id=chat,
                                                        kinds=TERMINAL_KINDS)
    return [ev for ev in events if ev.kind == rn.NOTICE_KIND]


@pytest.fixture(autouse=True)
def _no_pull(monkeypatch):
    monkeypatch.setattr(client, "pull", lambda *a, **kw: None)


def test_one_comment_and_one_delivery_per_revision_across_passes(conn):
    tid = _card(conn)
    _subscribe(conn, tid)
    hub = _hub()
    for _ in range(4):
        assert wp.reevaluate(conn, hub, "default", "hub", cfg=CFG) == 1
    comments = _notice_comments(conn, tid)
    assert len(comments) == 1
    body = comments[0].body
    assert "(needs: skill:five-whys)" in body
    assert f"`{COMMAND.format(tid=tid)}` on hub" in body
    assert body.endswith("or until a pool worker is given those labels.")
    delivered = _delivered(conn, tid)
    assert len(delivered) == 1
    for _ in range(2):  # later passes never add another deliverable notice
        wp.reevaluate(conn, hub, "default", "hub", cfg=CFG)
    assert _delivered(conn, tid) == []
    assert len(_notices(conn, tid)) == 1


def test_notice_text_reaches_gateway_and_tui_chats(conn):
    from gateway import kanban_watchers_notifier as notifier
    from tui_gateway import session_notifications as tui

    tid = _card(conn)
    _subscribe(conn, tid)
    wp.reevaluate(conn, _hub(), "default", "hub", cfg=CFG)
    ev = _delivered(conn, tid)[0]
    assert rn.NOTICE_KIND not in notifier._WAKE_KINDS  # passive: the creator acts, nothing is auto-run
    assert not notifier.diagnostic_event(ev)  # an owner ask, not infrastructure noise
    text = notifier._EVENT_FORMATTERS[rn.NOTICE_KIND](ev, SimpleNamespace(head=f"Kanban {tid}"))[0]
    assert text.startswith(f"⚠ Kanban {tid}") and COMMAND.format(tid=tid) in text
    assert rn.NOTICE_KIND in tui._KANBAN_NOTIFY_KINDS
    _glyph, fmt = tui._KANBAN_EVENT_FORMATTERS[rn.NOTICE_KIND]
    assert COMMAND.format(tid=tid) in fmt(None, ev.payload, "title")


def test_card_without_origin_chat_still_gets_the_comment(conn):
    tid = _card(conn)
    wp.reevaluate(conn, _hub(), "default", "hub", cfg=CFG)
    assert len(_notice_comments(conn, tid)) == 1
    assert conn.execute("SELECT count(*) FROM kanban_notify_subs WHERE task_id=?", (tid,)).fetchone()[0] == 0


def test_labels_fall_back_to_the_cards_required_labels(conn):
    tid = _card(conn)
    wp.reevaluate(conn, _hub(_queued(missing=())), "default", "hub", cfg=CFG)
    body = _notice_comments(conn, tid)[0].body
    assert "(needs: profile:default, capability:terminal, capability:file, skill:five-whys)" in body


def test_new_card_revision_gets_a_new_notice(conn):
    tid = _card(conn)
    hub = _hub()
    wp.reevaluate(conn, hub, "default", "hub", cfg=CFG)
    conn.execute("UPDATE kanban_sync_state SET applying=1")
    conn.execute("UPDATE tasks SET skills=? WHERE id=?", (json.dumps(["five-whys", "plan"]), tid))
    conn.execute("UPDATE kanban_sync_state SET applying=0")
    conn.execute("DELETE FROM kanban_sync_outbox")
    conn.commit()
    hub.reserve.return_value = _queued(missing=("skill:five-whys", "skill:plan"))
    for _ in range(2):
        wp.reevaluate(conn, hub, "default", "hub", cfg=CFG)
    assert len(_notice_comments(conn, tid)) == 2


def test_no_repin_call_is_made_and_the_fence_stays(conn):
    tid = _card(conn)
    hub = _hub()
    for _ in range(3):
        wp.reevaluate(conn, hub, "default", "hub", cfg=CFG)
    assert len(_notice_comments(conn, tid)) == 1  # the owner was told...
    hub.repin.assert_not_called()  # ...and nothing repinned it for them
    row = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    assert row["placement_policy"] == wp.POLICY and row["runs_on"] is None
    assert wp.execution_refusal(conn, row) == wp.WAITING
    assert not dispatch.has_spawnable_ready(conn)


def test_already_queued_card_gets_its_notice_on_the_next_pass(conn, monkeypatch):
    tid = _card(conn)
    _subscribe(conn, tid)
    hub = _hub()
    with monkeypatch.context() as m:  # the pass that ran before this code was installed
        m.setattr(rn, "maybe_notify", lambda *a, **kw: False)
        wp.reevaluate(conn, hub, "default", "hub", cfg=CFG)
    assert qr.latest(conn, tid)["code"] == "no_eligible_worker"
    assert _notice_comments(conn, tid) == []
    wp.reevaluate(conn, hub, "default", "hub", cfg=CFG)
    assert len(_notice_comments(conn, tid)) == 1
    assert len(_delivered(conn, tid)) == 1


def test_other_queue_codes_post_no_notice(conn):
    tid = _card(conn)
    reply = _queued()
    reply["observation"]["code"] = "workers_saturated"
    wp.reevaluate(conn, _hub(reply), "default", "hub", cfg=CFG)
    assert _notices(conn, tid) == []


def test_stuck_summary_names_the_card_and_does_not_count_it_as_a_stall(conn):
    tid = _card(conn)
    wp.reevaluate(conn, _hub(), "default", "hub", cfg=CFG)
    rows = conn.execute("SELECT * FROM tasks WHERE status='ready'").fetchall()
    result = dispatch.DispatchResult()
    assert eligible_rows(conn, rows, "ready", result) == []
    assert result.skipped_ineligible == [(tid, rn.MANUAL_REPIN)]
    assert "skipped_ineligible" not in describe_suppression([result])
    line = stuck_warning(6, [result])
    assert f"waiting for a manual repin: {tid}" in line
    assert "Board lease refused or deferred" not in line  # not blamed on the lease
    assert not dispatch.has_spawnable_ready(conn)


def test_stuck_summary_keeps_other_refusals_as_lease_holds(conn):
    tid = _card(conn)
    result = dispatch.DispatchResult()
    result.skipped_ineligible = [(tid, rn.MANUAL_REPIN), ("t_other", "labels_mismatch")]
    line = stuck_warning(6, [result])
    assert f"waiting for a manual repin: {tid}" in line
    assert "skipped_ineligible(labels_mismatch)=1" in line
    assert "Board lease refused or deferred" in line


# Live shape (hub, 2026-11-04, fixture t_01a1a46cbd08b275): with worker overflow off the Board never computes
# ``no_eligible_worker`` (only ``overflowDecision`` does); a card no pool worker is attested for queues with the
# per-candidate rollup ``capability_mismatch`` (or ``mixed``) instead. It is the same silent wait.

def _rolled_up(code, *candidates):
    reply = _queued()
    reply["observation"]["code"] = code
    reply["observation"]["candidates"] = list(candidates)
    return reply


_W1_MISSING = {"machine": "worker-1", "reasons": ["missing_labels"], "missing_labels": ["skill:five-whys"]}


def test_capability_mismatch_over_the_whole_pool_gets_one_notice_and_no_second_comment(conn):
    tid = _card(conn)
    _subscribe(conn, tid)
    hub = _hub(_rolled_up("capability_mismatch", _W1_MISSING))
    for _ in range(3):
        wp.reevaluate(conn, hub, "default", "hub", cfg=CFG)
    ours = [c.body for c in kb.list_comments(conn, tid) if c.author == rn.AUTHOR]
    assert len(ours) == 1 and ours[0].startswith("No pool worker can run this card (needs: skill:five-whys)")
    assert COMMAND.format(tid=tid) in ours[0]
    assert len(_delivered(conn, tid)) == 1
    hub.repin.assert_not_called()


def test_mixed_rollup_where_every_pool_worker_lacks_a_capability_gets_the_notice(conn):
    tid = _card(conn)
    stale = {**_W1_MISSING, "reasons": ["last_seen_stale", "missing_labels"]}
    wp.reevaluate(conn, _hub(_rolled_up("mixed", stale)), "default", "hub", cfg=CFG)
    assert len(_notice_comments(conn, tid)) == 1


def test_a_pool_worker_without_a_capability_reason_means_no_notice(conn):
    tid = _card(conn)
    cfg = {"worker_pools": {"hub": ["worker-1", "worker-2"]}}
    busy = {"machine": "worker-2", "reasons": ["max_card_runs_reached"]}
    wp.reevaluate(conn, _hub(_rolled_up("mixed", _W1_MISSING, busy)), "default", "hub", cfg=cfg)
    assert _notices(conn, tid) == []


def test_a_pool_worker_missing_from_the_observation_means_no_notice(conn):
    tid = _card(conn)
    cfg = {"worker_pools": {"hub": ["worker-1", "worker-2"]}}
    wp.reevaluate(conn, _hub(_rolled_up("capability_mismatch", _W1_MISSING)), "default", "hub", cfg=cfg)
    assert _notices(conn, tid) == []
    assert any(c.body.startswith("Candidate capabilities") for c in kb.list_comments(conn, tid))


def test_stuck_summary_counts_a_capability_mismatch_card_as_waiting_for_repin(conn):
    tid = _card(conn)
    wp.reevaluate(conn, _hub(_rolled_up("capability_mismatch", _W1_MISSING)), "default", "hub", cfg=CFG)
    rows = conn.execute("SELECT * FROM tasks WHERE status='ready'").fetchall()
    result = dispatch.DispatchResult()
    assert eligible_rows(conn, rows, "ready", result) == []
    assert result.skipped_ineligible == [(tid, rn.MANUAL_REPIN)]
    assert f"waiting for a manual repin: {tid}" in stuck_warning(6, [result])
