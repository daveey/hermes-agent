"""Another window's Stop ends exactly one automated turn, with an ordinary interrupt. [daveey]

The asker records an end request for the exact durable holder (compare-and-set against ``session_turn_leases``);
the holding process's ``DurableTurnLease`` tick sees it and interrupts its agent the ordinary way, so the turn is
not a lease loss and the lease is released as usual (a prompt queued behind it then runs once).
"""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace

import pytest

from agent.turn_end_request import AUTO_KINDS, END_MESSAGE, auto_token, holder_auto_kind
from agent.turn_facade_lease import DurableTurnLease, lease_holder
from hermes_state import SessionDB

KEY = "20261121_060000_endreq"


@pytest.fixture
def db(tmp_path):
    handle = SessionDB(tmp_path / "state.db")
    handle.create_session(KEY, "desktop")
    yield handle
    handle.close()


def test_holder_names_only_automated_turns():
    auto = lease_holder("sid:task:abc", "tui", "kanban_notice")
    typed = lease_holder("sid:task:abc", "tui", None)
    assert holder_auto_kind(auto) == "kanban_notice" and auto.endswith(":platform=tui")
    assert auto.rsplit(":platform=", 1)[-1] == "tui"  # readers that take the surface from the tail still work
    assert holder_auto_kind(typed) is None and ":auto=" not in typed
    assert auto_token("loop_tick") == "" and auto_token("hidden") == "" and auto_token(None) == ""
    # A client-chosen session/task id cannot fake the stamp: only the segment before the last :platform= counts.
    assert holder_auto_kind(lease_holder("sid:task:auto=kanban_notice:x", "tui", None)) is None
    # relay ids are ``<session>:<task>:<8 hex>`` (agent/turn_facade.py): a chosen task id never ends the turn id
    assert holder_auto_kind(lease_holder("sid:auto=kanban_notice:1a2b3c4d", "tui", None)) is None
    assert holder_auto_kind("pid=1:turn=t:auto=kanban_notice") is None  # no :platform= tail
    assert {"kanban_notice", "auto_continue", "process_complete"} <= AUTO_KINDS and "loop_tick" not in AUTO_KINDS


def test_request_turn_end_is_a_compare_and_set_on_the_exact_holder(db):
    holder = lease_holder("t1", "tui", "kanban_notice")
    other = lease_holder("t2", "tui", "kanban_notice")
    assert db.request_turn_end(KEY, holder) is False  # nobody holds the lease
    assert db.acquire_session_turn_lease(KEY, holder, ttl_seconds=60, wait_seconds=0)

    assert db.request_turn_end(KEY, other) is False  # not the turn that holds it now
    assert db.turn_end_requested(KEY, holder) is False
    assert db.request_turn_end(KEY, holder, reason="stop") is True
    assert db.turn_end_requested(KEY, holder) is True and db.turn_end_requested(KEY, other) is False

    db.release_session_turn_lease(KEY, holder)
    assert db.acquire_session_turn_lease(KEY, other, ttl_seconds=60, wait_seconds=0)
    assert db.turn_end_requested(KEY, other) is False  # an old request never reaches the next turn


class _Agent:
    def __init__(self):
        self.interrupts = []
        self.session_id = KEY
        self._session_turn_lease_refresh_interval = 60.0
        self._execution_thread_id = None

    def interrupt(self, message=None, **kwargs):
        self.interrupts.append((message, kwargs))


def _lease(db, holder):
    lease = DurableTurnLease(_Agent(), db, KEY, holder)
    with lease._lock:
        lease.turn_active = True
    return lease


def test_the_holding_turn_ends_with_an_ordinary_interrupt_and_keeps_its_lease(db):
    holder = lease_holder("t1", "tui", "kanban_notice")
    assert db.acquire_session_turn_lease(KEY, holder, ttl_seconds=60, wait_seconds=0)
    lease = _lease(db, holder)

    assert lease.takeover_tick() is None and lease.agent.interrupts == []  # no request yet
    assert db.request_turn_end(KEY, holder)
    assert lease.takeover_tick() is None  # keeps ticking: the turn unwinds, then releases
    assert lease.takeover_tick() is None

    assert lease.agent.interrupts == [(END_MESSAGE, {})]  # once, no hard cancel, no lease-lost reason
    assert db.session_turn_lease_owner(KEY)["holder"] == holder  # the lease was not taken away


def test_a_typed_turn_never_honours_a_request(db):
    holder = lease_holder("t1", "tui", None)
    assert db.acquire_session_turn_lease(KEY, holder, ttl_seconds=60, wait_seconds=0)
    assert db.request_turn_end(KEY, holder)  # even if one were recorded for it
    lease = _lease(db, holder)

    lease.takeover_tick()
    assert lease.agent.interrupts == []


def test_a_queued_turn_runs_once_after_the_ended_turn_releases(db):
    """The request ends the background turn; its normal release lets the forwarded turn waiting for the lease take
    it, exactly once."""
    background = f"pid={os.getpid()}:turn=closeout:auto=kanban_notice:platform=tui"
    forwarded = f"pid={os.getpid()}:turn=forwarded:platform=tui"
    assert db.acquire_session_turn_lease(KEY, background, ttl_seconds=60, wait_seconds=0)
    lease = _lease(db, background)
    got, waiting = [], threading.Event()
    worker = threading.Thread(target=lambda: got.append(db.acquire_session_turn_lease(
        KEY, forwarded, ttl_seconds=60, wait_seconds=10, poll_interval_seconds=0.02,
        on_wait=lambda elapsed: waiting.set())))
    worker.start()
    assert waiting.wait(5)

    assert db.request_turn_end(KEY, background)
    lease.takeover_tick()
    assert lease.agent.interrupts  # the turn was asked to stop ...
    lease.release()  # ... unwinds, and releases as usual
    worker.join(10)

    assert got == [True] and db.session_turn_lease_owner(KEY)["holder"] == forwarded


def test_a_store_without_the_table_reads_no_request(tmp_path):
    store = SimpleNamespace(turn_end_requested=lambda *a: (_ for _ in ()).throw(RuntimeError("old store")))
    lease = DurableTurnLease(_Agent(), store, KEY, "pid=1:turn=t:auto=kanban_notice:platform=tui")
    with lease._lock:
        lease.turn_active = True
    assert lease._end_requested() is False
