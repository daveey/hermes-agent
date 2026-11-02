"""A delegation result held for its busy owner's poller (``_notif_held``) stays visible to the orphan sweep.

The fork hands an owner-addressed event to its owner's poller instead of the shared completion queue
(``_notif_hold_for``). Upstream's orphan sweep counts a result copy as live only while it is queued or marked
held (``async_delegation.mark_copy_held``). Without a mark for the held list, a result waiting out an owner's
turn longer than a claim lease reads as lost: the sweep warns and queues a duplicate every lease. The mark must
cover the whole custody: handoff, ``_notif_take_held`` through routing, and the shutdown drain's requeue.
"""

from __future__ import annotations

import collections
import contextlib
import logging
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from tui_gateway import notification_park, server

OWNER_KEY = "owner-session-key"
OTHER_KEY = "unrelated-session-key"


def _session(key: str, *, running: bool) -> dict:
    return {"session_key": key, "history_lock": threading.RLock(), "running": running, "history": [],
            "agent": None, "profile_home": "", "_finalized": False, "_notif_held": []}


def _registry() -> SimpleNamespace:
    return SimpleNamespace(completion_queue=queue.Queue(), is_completion_consumed=lambda _sid: False,
                           restore_completions=lambda: 0)


class _StopAfter:
    def __init__(self, checks: int):
        self._left = checks

    def is_set(self):
        self._left -= 1
        return self._left < 0


@pytest.fixture
def held(tmp_path, monkeypatch, caplog):
    """A real delegation ledger whose finished result a foreign poller handed to its busy owner's held list."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tests.waiting import wait_until
    from tools import async_delegation as ad
    from tools.process_registry_awaiting import release, take_ready

    monkeypatch.setattr(notification_park, "_parked", [])
    monkeypatch.setattr(ad, "_held_copies", collections.Counter())

    @contextlib.contextmanager
    def _fake_session_db(_session_):
        yield SimpleNamespace(resolve_resume_session_id=lambda key: key)

    monkeypatch.setattr(server, "_session_db", _fake_session_db)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    for name in ("_maybe_fire_tui_loop_tick", "_maybe_fire_tui_heartbeat_tick", "_notif_poll_kanban"):
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    monkeypatch.setattr(ad, "maybe_sweep_orphaned_completions", lambda q: 0)
    monkeypatch.setattr("tui_gateway.producer_admission.deliver_pending", lambda *a, **k: None)
    registry = _registry()
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    home = tmp_path / "home"
    home.mkdir()
    ad._reset_for_tests()
    token = set_hermes_home_override(str(home))
    try:
        handle = ad.dispatch_async_delegation(
            goal="mine", context=None, toolsets=None, role="leaf", model="m", session_key=OWNER_KEY,
            runner=lambda: {"status": "completed", "summary": "done"})
        wait_until(lambda: ad.active_count() == 0, timeout=10, what="delegation finished")
        owner, other = _session(OWNER_KEY, running=True), _session(OTHER_KEY, running=False)
        monkeypatch.setattr(server, "_sessions", {"owner-sid": owner, "other-sid": other})
        ready = take_ready(registry.completion_queue, 0)  # a foreign poller drains the owner's result...
        server._notif_handle_ready("other-sid", other, ready, set(), registry, lambda _evt: "text", None)
        release(ready)  # ...and hands it to the busy owner's poller
        delegation_id = handle["delegation_id"]
        assert [evt["delegation_id"] for evt in owner["_notif_held"]] == [delegation_id]
        assert registry.completion_queue.empty()
        caplog.set_level(logging.WARNING, logger="tools.async_delegation")
        yield SimpleNamespace(ad=ad, registry=registry, owner=owner, delegation_id=delegation_id, caplog=caplog,
                              lease=time.time() + ad._CLAIM_LEASE_S + 1)
    finally:
        reset_hermes_home_override(token)
        ad._reset_for_tests()


def _reoffers(caplog) -> list:
    return [r for r in caplog.records if "Re-offering" in r.getMessage()]


def test_the_sweep_counts_a_held_result_as_live_past_a_lease(held):
    sweep_q: queue.Queue = queue.Queue()

    assert held.ad.sweep_orphaned_completions(sweep_q, now=held.lease) == 0
    assert sweep_q.empty() and not _reoffers(held.caplog)


def test_the_held_mark_lasts_through_take_held_routing_then_ends(held, monkeypatch):
    """The owner goes idle: its poller takes the held list and runs the turn. The copy stays marked until routing
    returns (no window where it is neither queued nor held), and the mark ends with delivery."""
    seen_live: list = []

    def _submit(_rid, sid, session, text, **_k):
        seen_live.append(held.delegation_id in held.ad._queued_completion_ids(held.registry.completion_queue))
        return True

    monkeypatch.setattr(server, "_run_prompt_submit", _submit)
    held.owner["running"] = False

    server._notification_poller_loop(_StopAfter(1), "owner-sid", held.owner)

    assert seen_live == [True]
    assert not held.ad._held_copies


def test_the_shutdown_drain_requeues_a_held_result_before_its_mark_ends(held, monkeypatch):
    """A busy owner's poller stops: the held result goes back to the shared queue, where the sweep still sees it,
    and no held mark outlives the list."""
    put = held.registry.completion_queue.put
    marked_at_requeue: list = []

    def _put(evt, *a, **k):
        marked_at_requeue.append(held.ad._held_copies.get(held.delegation_id, 0))
        return put(evt, *a, **k)

    monkeypatch.setattr(held.registry.completion_queue, "put", _put)
    stop = threading.Event()
    stop.set()

    server._notification_poller_loop(stop, "owner-sid", held.owner)

    assert "_notif_held" not in held.owner
    assert [evt["delegation_id"] for evt in held.registry.completion_queue.queue] == [held.delegation_id]
    assert marked_at_requeue and all(marked_at_requeue)
    assert not held.ad._held_copies
    sweep_q: queue.Queue = queue.Queue()
    assert held.ad.sweep_orphaned_completions(sweep_q, now=held.lease) == 0 and sweep_q.empty()
