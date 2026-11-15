"""A claim-time hub admission deny never leaves a card waiting on the hub with no ``runs_on``. [daveey]

Incident (11-13, hermes-hub, t_01a184cee94603eb "Standing: support Eve on Ask Eve"): the hub's claim screen denied
it at 01:40Z (a worker with ssh + hub-admin can do it) but recorded ``kept: wake_check reads a hub-local path``,
because its wake_check runs ``/home/ubuntu/.hermes/bin/alpha-hub-escalation-check.py``. That probe is portable: the
fleet installs it at the same path on every worker, and off the hub it delegates to the hub copy through hub-admin.
The card was never re-placed, so the hub claimed and released it (``unused_grant``) for ~3 h while workers had
free slots. Two fixes:

* ``keep_reason`` ignores a ``~/.hermes/bin/<name>`` probe the fleet installs on every worker
  (``PORTABLE_WAKE_PROBES``, overridable by ``kanban.placement.portable_wake_probes``);
* the dispatcher self-check (``kanban_worker_first_selfcheck.denied_target``, run by the claim screen) re-places a
  ready, label-less card created here whose latest verdict is a deny that no keep reason holds for now, once it has
  waited 5 min and a live worker carries the queue labels. A ``kept`` that still holds keeps the card.
"""
from __future__ import annotations

import logging
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_hub_admission_claim as hac
from hermes_cli import kanban_parent_unclaimed as unclaimed
from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_worker_first_selfcheck as selfcheck
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import hub_judge_support
from tests.hermes_cli import test_kanban_hub_admission_pickup as pickup_suite
from tests.hermes_cli.test_kanban_worker_node import BOARD, HUB, WORKER

hub_judge = hub_judge_support.hub_judge
gws = pickup_suite.gws
POOL = pickup_suite.POOL
PROBE_NAME = "alpha-hub-escalation-check.py"
PROBE = f"/usr/bin/python3 -S /home/ubuntu/.hermes/bin/{PROBE_NAME} --after 1794533719.848279"
STALE_KEPT = {"admit": False, "entry": "claim", "why": "a worker with ssh + hub-admin can do it",
              "kept": "wake_check reads a hub-local path", "runs_on": None, "created_on": HUB}


def _events(conn, tid, kind):
    return pickup_suite._events(conn, tid, kind)


def _holder(svc, tid):
    return (svc.board(BOARD)["leases"].get(tid) or {}).get("holder")


@pytest.fixture
def portable(monkeypatch):
    cfg = {**pickup_suite.CFG, "portable_wake_probes": [PROBE_NAME]}
    monkeypatch.setattr(kp, "config", lambda: cfg)
    return cfg


def _card(hub, *, wake_check=None, verdict=None, **kw):
    hub.enter()
    tid = kb.create_task(hub.conn, title="Standing: support Eve on Ask Eve",
                         body="Answer the hand-offs Alpha posts for Eve.", assignee="default", **kw)
    with kb.write_txn(hub.conn):
        if wake_check:
            kb._append_event(hub.conn, tid, "scheduled", {"wake_check": wake_check})
        if verdict:
            kb._append_event(hub.conn, tid, "hub_admission", verdict)
    return tid


def _waited(monkeypatch, seconds):
    monkeypatch.setattr(unclaimed, "ready_since", lambda conn, tid, created_at=None: int(time.time()) - seconds)


def _tick(hub):
    hub.round()
    return hub.tick()


# --- keep_reason: a portable fleet probe is not a hub-local wake_check ------------------------------------------

def test_a_claim_deny_with_a_portable_fleet_probe_moves_to_the_worker_queue(gws, hub_judge, monkeypatch):
    """The live t_01a184cee94603eb shape, with the default probe list: red on main (kept on the hub)."""
    from tests.hermes_cli.test_kanban_hub_admission_claim import _no_board_promotion
    hub, worker, svc = gws
    _no_board_promotion(monkeypatch)  # the claim screen's path; promotion applies the same keep reasons
    hub_judge.admit_next = False
    tid = _card(hub, wake_check=PROBE)
    _tick(hub)
    [verdict] = _events(hub.conn, tid, "hub_admission")
    assert verdict["admit"] is False and "kept" not in verdict
    [moved] = _events(hub.conn, tid, "moved")
    assert moved["runs_on"][0] == POOL and _holder(svc, tid) != HUB
    hub.round()
    worker.round()
    worker.tick()
    assert _holder(svc, tid) == WORKER


@pytest.mark.parametrize("probe", [
    f"{PROBE} && test -f ~/.hermes/cache/ready.flag",  # another hub path in the same command still keeps it
    "python3 /home/ubuntu/.hermes/bin/mini-ssh.py 'true'",  # not listed as portable
    f"python3 ~/.hermes/bin/{PROBE_NAME}.bak",  # a different file that only starts with the name
])
def test_hub_local_probes_still_keep_a_denied_card(gws, hub_judge, portable, probe):
    hub, _worker, svc = gws
    hub_judge.admit_next = False
    tid = _card(hub, wake_check=probe)
    _tick(hub)
    [verdict] = _events(hub.conn, tid, "hub_admission")
    assert verdict["kept"] == "wake_check reads a hub-local path" and _holder(svc, tid) == HUB


def test_an_empty_portable_probe_list_makes_a_bin_probe_hub_local(gws, hub_judge, monkeypatch):
    hub, _worker, svc = gws
    monkeypatch.setattr(kp, "config", lambda: {**pickup_suite.CFG, "portable_wake_probes": []})
    hub_judge.admit_next = False
    tid = _card(hub, wake_check=PROBE)
    _tick(hub)
    [verdict] = _events(hub.conn, tid, "hub_admission")
    assert verdict["kept"] == "wake_check reads a hub-local path" and _holder(svc, tid) == HUB


# --- self-check: an earlier deny left on the hub is re-placed ---------------------------------------------------

@pytest.mark.parametrize("case", ["stale_kept", "no_kept"])
def test_self_check_repins_a_denied_card_left_ready_on_the_hub(gws, hub_judge, portable, monkeypatch, caplog, case):
    """Red on main: a recorded deny is never revisited, so the hub claims the card."""
    hub, worker, svc = gws
    verdict = STALE_KEPT if case == "stale_kept" else {k: v for k, v in STALE_KEPT.items() if k != "kept"}
    tid = _card(hub, wake_check=PROBE, verdict=verdict)
    _waited(monkeypatch, 3 * 3600)
    with caplog.at_level(logging.WARNING, logger=selfcheck.__name__):
        counts = hac.screen(hub.conn)
    [moved] = _events(hub.conn, tid, "moved")  # on main: none, the hub claims the card
    assert counts.repinned == 1 and hub_judge.calls == []  # no new verdict, no model call
    assert moved["runs_on"][0] == POOL and moved["previous_runs_on"] is None and moved["queued"] is True
    assert moved["self_check"] == "hub_admission_denied"
    assert moved["reason"].startswith("hub admission denied before claim:")
    assert "a worker with ssh + hub-admin can do it" in moved["reason"]
    assert ("no longer holds" in moved["reason"]) is (case == "stale_kept")
    assert f"repinned {tid}" in caplog.text
    assert len(_events(hub.conn, tid, "hub_admission")) == 1
    _tick(hub)
    assert _holder(svc, tid) != HUB
    hub.round()
    worker.round()
    worker.tick()
    assert _holder(svc, tid) == WORKER


def _fetch_down():
    raise OSError("board unreachable")


def _linux_only():
    return {"me": HUB, "machines": [{**m, "labels": ["linux"]} for m in pickup_suite.MACHINES.values()]}


# case -> (card kwargs, verdict overrides or a whole verdict, wake_check, waited seconds, cfg override, registry)
_STAY = {

    "kept_holds": ({}, {}, "test -f ~/.hermes/cache/ready.flag", 3 * 3600, None, None),
    "dir_workspace": ({"workspace_kind": "dir"}, {"kept": "host workspace"}, PROBE, 3 * 3600, None, None),
    "host_repo": ({"requires": ["terminal", "host_repo"]}, {"kept": "uncarried requirement"}, PROBE, 3 * 3600,
                  None, None),
    "forced": ({}, {"forced": True}, PROBE, 3 * 3600, None, None),
    "admitted": ({}, {"admit": True, "need": "hub_chrome", "evidence": "x", "why": "ok", "kept": None}, PROBE,
                 3 * 3600, None, None),
    "no_machine": ({}, {}, PROBE, 3 * 3600, None, _linux_only),
    "registry_down": ({}, {}, PROBE, 3 * 3600, None, _fetch_down),
    "no_queue": ({}, {}, PROBE, 3 * 3600, {"worker_pools": {HUB: [WORKER]}, "portable_wake_probes": [PROBE_NAME]},
                 None),
}


@pytest.mark.parametrize("case", sorted(_STAY))
def test_self_check_leaves_other_cards_on_the_hub(gws, hub_judge, portable, monkeypatch, tmp_path, case):
    hub, _worker, _svc = gws
    kw, overrides, probe, waited, cfg, fetch = _STAY[case]
    if kw.get("workspace_kind") == "dir":
        kw = {**kw, "workspace_path": str(tmp_path / "ws")}
    if cfg is not None:
        monkeypatch.setattr(kp, "config", lambda: cfg)
    verdict = {k: v for k, v in {**STALE_KEPT, **overrides}.items() if v is not None}
    tid = _card(hub, wake_check=probe, verdict=verdict, **kw)
    _waited(monkeypatch, waited)
    if fetch is not None:
        hub.enter()
        wn.set_registry_fetch(fetch)
        wn._REGISTRY.update({"at": None, "value": None})
    assert hac.screen(hub.conn).repinned == 0, case
    assert _events(hub.conn, tid, "moved") == [], case
    assert hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0] is None, case


def test_a_failed_self_check_still_screens_the_unjudged_cards(gws, hub_judge, portable, monkeypatch, caplog):
    hub, _worker, _svc = gws
    denied = _card(hub, wake_check=PROBE, verdict=STALE_KEPT)
    hub_judge.admit_next = False
    fresh = _card(hub)

    def broken(*args, **kwargs):
        raise RuntimeError("registry exploded")
    monkeypatch.setattr(hac, "_replace", broken)
    with caplog.at_level(logging.WARNING, logger=hac.__name__):
        counts = hac.screen(hub.conn)
    assert counts.judged == 1 and counts.moved == 1 and len(_events(hub.conn, fresh, "moved")) == 1
    assert "denied-card self-check failed" in caplog.text
    assert denied in counts.deferred_ids


def test_self_check_repins_a_card_whose_only_verdict_is_in_its_created_payload(gws, hub_judge, portable,
                                                                             monkeypatch):
    """The screen gates on ``pickup.verdict`` (the ``created`` payload's audit when no ``hub_admission`` event
    exists), so the self-check reads the same verdict: red when it read only the events. ``record_created`` mirrors
    the audit as an event (all 136 creation-time denies on the hub, 11-13, have one), so this is the fallback for a
    card whose event is missing, not the usual path."""
    import json
    hub, _worker, _svc = gws
    tid = _card(hub, wake_check=PROBE)
    verdict = {k: v for k, v in STALE_KEPT.items() if k != "kept"}
    with kb.write_txn(hub.conn):
        (raw,) = hub.conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created'",
                                  (tid,)).fetchone()
        hub.conn.execute("UPDATE task_events SET payload = ? WHERE task_id = ? AND kind = 'created'",
                         (json.dumps({**json.loads(raw), "hub_admission": verdict}), tid))
    assert _events(hub.conn, tid, "hub_admission") == []
    _waited(monkeypatch, 3 * 3600)
    assert hac.screen(hub.conn).repinned == 1 and hub_judge.calls == []
    [moved] = _events(hub.conn, tid, "moved")
    assert moved["runs_on"][0] == POOL and moved["self_check"] == "hub_admission_denied"


@pytest.mark.parametrize("case", ["dir_workspace", "host_repo"])
def test_a_structurally_kept_card_skips_the_ready_since_read(gws, hub_judge, portable, monkeypatch, tmp_path, case):
    """Per tick the self-check runs for every judged card still here; a keep reason that holds is read before the
    ``ready_since`` events + runs scan, so a card kept for good costs no history read (red: read first)."""
    hub, _worker, _svc = gws
    kw, overrides, probe, *_ = _STAY[case]
    if case == "dir_workspace":
        kw = {**kw, "workspace_path": str(tmp_path / "ws")}
    tid = _card(hub, wake_check=probe, verdict={**STALE_KEPT, **overrides}, **kw)
    reads = []
    monkeypatch.setattr(unclaimed, "ready_since", lambda conn, t, created_at=None: reads.append(t) or 0)
    assert hac.screen(hub.conn).repinned == 0 and reads == []
    assert _events(hub.conn, tid, "moved") == []


def test_self_check_skips_a_card_that_changed_while_checked(gws, portable, monkeypatch):
    hub, _worker, _svc = gws
    tid = _card(hub, wake_check=PROBE, verdict=STALE_KEPT)
    _waited(monkeypatch, 3 * 3600)
    row = hub.conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    with kb.write_txn(hub.conn):
        kb._append_event(hub.conn, tid, "commented", {"author": "x"})
    head = hac._event_head(hub.conn, tid) - 1
    assert hac._replace(hub.conn, HUB, tid, [POOL], "r", expected=row, event_head=head) is False
    assert _events(hub.conn, tid, "moved") == []
