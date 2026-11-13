"""A placed card's capability owner is not a duplicate of its origin placeholder (t_01a1d4dc5eb85b00). [daveey]

Replays hub t_01a1d49003c65ca4: macbook forwarded card t_01a1d48f676074bc to the hub by deferred placement and
parked it on the hub copy. The hub worker hit a MacBook-only step and blocked with needs=['host:macbook'];
macbook refused the owner create as create_overlap with that placeholder (same title, it waits on the hub card
by construction), no owner card existed, and wait-check exited 2 until the card was woken wake_check_broken.
"""
from __future__ import annotations

import json
import time

from hermes_cli import kanban_capability_routing as kcr
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_overlap
from hermes_cli import kanban_placement_forward as forward
from tests.hermes_cli import test_kanban_capability_routing as base

env = base.env
_apply_owner_create, _cli, publish = base._apply_owner_create, base._cli, base.publish

TITLE = ("Hub session writes during gateway startup return 503 \"owner_unavailable: starting\" "
         "instead of riding out the startup window")
NEED = "host:macbook"
REASON = "Live proof needs the MacBook Desktop hermes:api IPC and its probe scripts."


def _set(conn, tid: str, status: str) -> None:
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))


def _park_on(conn, tid: str, ref: str, gateway: str = "hub") -> None:
    _set(conn, tid, "scheduled")
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "scheduled", {"awaiting": {"kind": "card", "ref": ref, "gateway": gateway},
                                                  "reason": "deferred placement"})


def _replicate(hub, gw: str, tid: str, title: str, status: str) -> None:
    hub.execute("INSERT OR REPLACE INTO replica VALUES (?, 'tasks', ?, 1, 1, ?, ?)",
                (gw, f"default/default/{tid}", json.dumps({"id": tid, "board": "default", "gateway": gw,
                                                           "status": status, "title": title}), int(time.time())))
    hub.commit()


def _placed_on_hub(env):
    """The hub copy of a macbook card (body built by the real forward), with the macbook placeholder replicated
    as the hub sees it. Returns (hub card, macbook placeholder)."""
    with kbc.connect_closing() as conn:
        # Build the forwarded body from a stand-in for macbook's card, then retire the stand-in: on the hub the
        # placeholder exists only in the replica.
        placeholder = kb.create_task(conn, title=TITLE, body="Fix spec: retry while starting.", assignee="default")
        body = forward.remote_args(conn, kb.get_task(conn, placeholder), self_gw="macbook")["body"]
        _set(conn, placeholder, "archived")
        hub_card = kb.create_task(conn, title=TITLE, body=body, assignee="default")
        _set(conn, hub_card, "ready")
        assert kb.claim_task(conn, hub_card, claimer="worker") is not None
    _replicate(env["hub"], "macbook", placeholder, TITLE, "scheduled")
    return hub_card, placeholder


# --- 1. the owner create is not refused against the requester's own placeholder -------------------------


def test_placed_card_routes_host_need_to_its_origin_and_the_owner_is_created(env, monkeypatch):
    """Acceptance 1: a hub-placed card whose origin placeholder is scheduled on macbook routes needs=['host:macbook']
    and the owner card IS created on macbook (on main: create_overlap with the placeholder)."""
    publish(env["hub"], "macbook", {NEED: time.time()})
    applied = _apply_owner_create(env, monkeypatch)
    hub_card, placeholder = _placed_on_hub(env)
    with kbc.connect_closing() as conn:
        out = kcr.route(conn, hub_card, [NEED], reason=REASON)
        task = kb.get_task(conn, hub_card)
        assert task is not None and task.status == "scheduled"
    assert not applied.get("rejected"), applied
    assert applied.get("owner"), applied
    assert placeholder in applied["matches"] and placeholder in applied["distinct"], applied
    assert out["routes"][0]["gateway"] == "macbook"
    rc, msg = kcr.wait_check(kcr.request_id(hub_card, NEED))
    assert rc == 1, msg  # pending on the owner card, not a rejection


def test_genuine_twin_on_the_origin_gateway_still_refuses_the_owner(env, monkeypatch):
    """Only the bound placeholder is exempt: another open card on macbook with the same title still refuses."""
    publish(env["hub"], "macbook", {NEED: time.time()})
    applied = _apply_owner_create(env, monkeypatch)
    hub_card, placeholder = _placed_on_hub(env)
    stranger = "t_01a1d4ffffff0001"
    _replicate(env["hub"], "macbook", stranger, TITLE, "running")
    with kbc.connect_closing() as conn:
        try:
            kcr.route(conn, hub_card, [NEED], reason=REASON)
        except Exception as exc:  # the synchronous fake rejects before the park
            applied["error"] = str(exc)
    assert applied.get("rejected"), applied
    assert stranger in applied["matches"] and stranger not in applied["distinct"], applied
    assert placeholder in applied["distinct"], applied


def test_bound_cards_reads_the_forward_footer_and_local_parks(env):
    with kbc.connect_closing() as conn:
        hub_card = kb.create_task(conn, title=TITLE, assignee="default",
                                  body="x\n\nThis card was macbook card t_01a1d48f676074bc; it completes there "
                                       "when this one is done.\n\n---\nPlaced on hub by macbook's rule.")
        local_waiter = kb.create_task(conn, title=TITLE, assignee="default")
        _park_on(conn, local_waiter, hub_card)
        unrelated = kb.create_task(conn, title=TITLE, assignee="default")
        _park_on(conn, unrelated, "t_01a1d4aaaaaa0000")
        assert kcr.bound_cards(conn, hub_card) == ["t_01a1d48f676074bc", local_waiter]
        excl = kcr.overlap_exclusions(conn, hub_card)
    assert excl[0] == hub_card and "t_01a1d48f676074bc" in excl and local_waiter in excl
    assert unrelated not in excl


def _owner_text(waiting: str) -> tuple[str, str]:
    return (f"Capability owner: {NEED} for hub/{waiting} ({TITLE[:80]})",
            f"Capability owner card for waiting card hub/default/default/{waiting} (\"{TITLE}\").\n")


def test_holder_skips_its_placeholder_even_without_distinct_from(env):
    """Target side (an older requester sends no distinct_from for it): macbook's overlap scan must not count its
    own placeholder, bound by placement_forwarded or by its park, while a same-title stranger still matches."""
    waiting = "t_01a1d49003c65ca4"
    with kbc.connect_closing() as conn:
        forwarded = kb.create_task(conn, title=TITLE, assignee="default")
        with kb.write_txn(conn):
            kb._append_event(conn, forwarded, "placement_forwarded",
                             {"request_id": "placement:macbook:abc", "remote_task_id": waiting})
        _set(conn, forwarded, "scheduled")
        parked = kb.create_task(conn, title=TITLE, assignee="default")
        _park_on(conn, parked, waiting)
        stranger = kb.create_task(conn, title=TITLE, assignee="default")
        _park_on(conn, stranger, "t_01a1d4aaaaaa0000")
    title, body = _owner_text(waiting)
    ids = [m["id"] for m in kanban_overlap.find(title=title, body=body, target_board="default", session_id=None,
                                                self_tid=None, parents=[])]
    assert forwarded not in ids and parked not in ids, ids
    assert stranger in ids, ids
    # Not an owner create: the exemption needs the capability-owner shape, so a plain twin still matches.
    plain = [m["id"] for m in kanban_overlap.find(title=TITLE, body="", target_board="default", session_id=None,
                                                  self_tid=None, parents=[])]
    assert forwarded in plain and parked in plain


# --- 2. a rejected owner request wakes the card at once with the refusal ---------------------------------

# The hub's sent row for capability-owner:t_01a1d49003c65ca4:host:macbook (2026-11-13 12:05 UTC), abridged.
REFUSAL = json.dumps({"ok": False, "created": False, "code": "create_overlap", "matches": [
    {"id": "t_01a1d48f676074bc", "board": "default", "gateway": "macbook", "status": "scheduled", "title": TITLE}],
    "total_matches": 1, "note": "No card was created: 1 open card(s) already look like this work."})


def _rejected_request(env, rid: str) -> None:
    env["hub"].execute("INSERT INTO sent VALUES (?, ?, 'rejected', ?, 0, 0, 1)",
                       (rid, json.dumps({"to_gw": "macbook", "key": "default/default/new"}),
                        json.dumps({"error": REFUSAL})))
    env["hub"].commit()


def test_rejected_owner_request_wakes_with_the_refusal_not_a_broken_probe(env, capsys):
    """Acceptance 2: exit 0 (the dispatcher wakes the card on the first probe) carrying the refusal, where main
    exited 2 three times and woke it wake_check_broken with "Fix the probe"."""
    rid = "capability-owner:t_01a1d49003c65ca4:host:macbook"
    _rejected_request(env, rid)
    rc, msg = kcr.wait_check(rid)
    assert rc == 0, msg
    assert "rejected by macbook" in msg and "create_overlap" in msg and "t_01a1d48f676074bc" in msg, msg
    assert _cli("wait-check", "capability-owner", rid) == 0
    assert "create_overlap" in capsys.readouterr().out
    # The park producer check still refuses parking on a dead request (owner_status keeps "not live").
    assert kcr.owner_status(rid)[0] == 2


def test_one_rejected_request_wakes_even_while_another_is_pending(env):
    pending = "capability-owner:t_01a1d49003c65ca4:ssh:mini"
    env["hub"].execute("INSERT INTO sent VALUES (?, ?, 'local', NULL, 0, 0, 0)",
                       (pending, json.dumps({"to_gw": "mini", "key": "default/default/new"})))
    rejected = "capability-owner:t_01a1d49003c65ca4:host:macbook"
    _rejected_request(env, rejected)
    rc, msg = kcr.wait_check(f"{pending},{rejected}")
    assert rc == 0 and rejected in msg, msg
