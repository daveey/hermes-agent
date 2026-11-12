"""A placement request the target rejected is terminal, never a clock park. [daveey]

Incident placement:worker-2:b80b2c801dd844db7b56cbf4: the MacBook refused the forwarded copy of a deferred card
with ``create_overlap``, naming only the forwarding card itself (the local placeholder, seen through the hub
replica). The forwarder treated only ``done`` as terminal and re-parked the card on a 10-minute clock forever.
Now a forward names the local card in ``distinct_from``; a self-match is re-forwarded once under a new request
id; an overlap with another card blocks for a decision; any other rejection runs the card here.
"""
from __future__ import annotations

import json
import time

import pytest

from tests.hermes_cli.test_kanban_placement import HUB_TID, NEW_HELP, _done_parent, _forwarder, _options

pytest_plugins = ["tests.hermes_cli.test_kanban_placement"]  # its ``mac`` fixture (fake hub + board)


def _reject(mac, rid, matches=None, *, error=None):
    """Mark hub request ``rid`` rejected the way hermes-hub 1.9 stores the target's refusal (incident shape)."""
    if matches is None:
        res = {"error": error or "no such profile/session here"}
    else:
        out = json.dumps({"ok": False, "created": False, "code": "create_overlap", "matches": matches,
                          "total_matches": len(matches),
                          "note": f"No card was created: {len(matches)} open card(s) already look like this work."})
        res = {"error": out + "\nWARNING: openrouter.inference is deprecated", "exit_code": 2, "stdout": out,
               "stderr": "WARNING: openrouter.inference is deprecated"}
    mac["hub"].execute("UPDATE sent SET state='rejected', result=?, done_at=? WHERE id=?",
                       (json.dumps(res), time.time(), rid))
    mac["hub"].commit()


def _match(tid):
    return {"id": tid, "board": "default", "gateway": "macbook", "status": "ready", "why": "titles share 5 words"}


def _scheduled_events(kb, conn, tid) -> int:
    return sum(e.kind == "scheduled" for e in kb.list_events(conn, tid))


@pytest.mark.parametrize("when", ["on_wake", "during_wait"])
@pytest.mark.parametrize("shape,action", [("self_overlap_old_client", "fallback"), ("other_overlap", "blocked"),
                                          ("plain", "fallback")])
def test_rejected_request_is_never_clock_parked(mac, monkeypatch, when, shape, action):
    """Incident placement:worker-2:b80b2c80: the target rejected the request, and the forwarder re-parked it on
    a 10-min clock forever because only ``done`` was terminal."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement as kp
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        parent = _done_parent(kb, conn)
    child = json.loads(kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]}))["task_id"]
    matches = {"self_overlap_old_client": [_match(child)], "other_overlap": [_match("t_0123456789abcdef")],
               "plain": None}[shape]
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        f = _forwarder(conn, child, mac, monkeypatch, await_tid=None)
        if when == "during_wait":
            def await_rejected(rid, **kw):
                _reject(mac, rid, matches)
                return kp.await_task_id(rid, hub_cli="hermes-hub", budget=30, sleep=_no_sleep)
            f.await_id = await_rejected
        else:
            assert f.run() == "requeued"  # queued, not answered yet: the 10-min clock is right here
            _reject(mac, kpf.state(conn, child)["request_id"], matches)
            kb.unblock_task(conn, child)
            kb.recompute_ready(conn)
            f = _forwarder(conn, child, mac, monkeypatch, await_tid=None)
            f.await_id = lambda rid, **kw: pytest.fail("waited 45 s on a request already rejected")
        before = _scheduled_events(kb, conn, child)
        assert f.run() == action
        assert _scheduled_events(kb, conn, child) == before
        assert kb.get_task(conn, child).status == {"fallback": "running", "blocked": "blocked"}[action]
        assert kpf.state(conn, child) is None  # deferral ended: the local worker (or an unblock) runs it here
        assert len(mac["calls"]) == 1  # an old client cannot send distinct_from, so no futile re-forward
        if action == "blocked":
            assert "t_0123456789abcdef" in json.dumps([e.payload for e in kb.list_events(conn, child)])


def _no_sleep(_s):
    raise AssertionError("await_task_id kept polling a rejected request")


def test_self_match_overlap_reforwards_once_with_distinct_from(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    mac["result"]["help"] = NEW_HELP
    with kbc.connect_closing() as conn:
        parent = _done_parent(kb, conn)
    child = json.loads(kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]}))["task_id"]
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        # The forward names the local placeholder up front, so the target's overlap guard never self-matches.
        f = _forwarder(conn, child, mac, monkeypatch, await_tid=None)
        assert f.run() == "requeued"
        first = mac["calls"][0]
        assert _options(first)["distinct_from"] == [child] and child in _options(first)["distinct_reason"]
        rid1 = first[first.index("--id") + 1]
        # A target that still refused it as an overlap with only this card (the legacy request in the incident
        # was sent without distinct_from): re-forward once, under a new request id.
        _reject(mac, rid1, [_match(child)])
        kb.unblock_task(conn, child)
        kb.recompute_ready(conn)
        f = _forwarder(conn, child, mac, monkeypatch, await_tid=HUB_TID)
        assert f.run() == "parked"
        assert len(mac["calls"]) == 2
        second = mac["calls"][1]
        rid2 = second[second.index("--id") + 1]
        assert rid2 != rid1 and _options(second)["distinct_from"] == [child]
        st = kpf.state(conn, child)
        assert st["request_id"] == rid2 and st["reforwarded"] and st["reforward_of"] == rid1
        assert st["remote_task_id"] == HUB_TID


def test_second_self_match_rejection_runs_the_card_here_without_a_third_forward(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    mac["result"]["help"] = NEW_HELP
    with kbc.connect_closing() as conn:
        parent = _done_parent(kb, conn)
    child = json.loads(kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]}))["task_id"]
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        f = _forwarder(conn, child, mac, monkeypatch, await_tid=None)
        f.await_id = lambda rid, **kw: (_reject(mac, rid, [_match(child)]), (None, None))[1]
        assert f.run() == "fallback"
        assert len(mac["calls"]) == 2  # the forward and one re-forward, never more
        assert kpf.state(conn, child) is None and kb.get_task(conn, child).status == "running"


def test_self_match_with_hidden_matches_is_not_treated_as_self_only(mac, monkeypatch):
    """``total_matches`` above the shown list: another card hides behind it, so no re-forward: a decision."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    mac["result"]["help"] = NEW_HELP
    with kbc.connect_closing() as conn:
        parent = _done_parent(kb, conn)
    child = json.loads(kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]}))["task_id"]
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        f = _forwarder(conn, child, mac, monkeypatch, await_tid=None)
        f.rejection = lambda rid: {"state": "rejected", "code": "create_overlap", "matches": [_match(child)],
                                   "total_matches": 4, "error": "4 open cards"}
        f.await_id = lambda rid, **kw: (_reject(mac, rid, [_match(child)]), (None, None))[1]
        assert f.run() == "blocked" and len(mac["calls"]) == 1
        assert "4 (not all named" in json.dumps([e.payload for e in kb.list_events(conn, child)])


def test_client_without_distinct_from_still_gets_the_plain_forward(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    mac["result"]["help"] = NEW_HELP
    with kbc.connect_closing() as conn:
        parent = _done_parent(kb, conn)
    child = json.loads(kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]}))["task_id"]
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        f = _forwarder(conn, child, mac, monkeypatch)
        real, sent = f.create, []

        def create(args, **kw):
            sent.append(dict(args))
            if "distinct_from" in args:
                return {"ok": False, "error": "create_option_unknown: distinct_from (allowed: skills, ...)"}
            return real(args, **kw)
        f.create = create
        assert f.run() == "parked"
        assert "distinct_from" in sent[0] and "distinct_from" not in sent[1] and "distinct_reason" not in sent[1]
        assert len(mac["calls"]) == 1


def test_remote_rejection_reads_the_incident_row(mac):
    from hermes_cli import kanban_placement as kp
    mac["hub"].execute("INSERT INTO sent (id, body, state, created_at) VALUES ('placement:x:1', '{}', 'sent', 0)")
    mac["hub"].commit()
    assert kp.remote_rejection("placement:x:1") == {}
    _reject(mac, "placement:x:1", [_match("t_01a1ce825d4f4a54")])
    why = kp.remote_rejection("placement:x:1")
    assert why["code"] == "create_overlap" and why["total_matches"] == 1
    assert [m["id"] for m in why["matches"]] == ["t_01a1ce825d4f4a54"] and "No card was created" in why["error"]
    _reject(mac, "placement:x:1", error="exit 1")
    assert kp.remote_rejection("placement:x:1") == {"state": "rejected", "code": None, "matches": [],
                                                     "total_matches": 0, "error": "exit 1"}
