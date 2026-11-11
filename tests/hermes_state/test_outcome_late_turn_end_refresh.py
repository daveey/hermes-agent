"""A turn-end refresh that runs late must not paint over a newer turn. [daveey]

PR1131 review (t_01a1ce099f1c7723): the gateway runs its turn-end refresh on an unawaited thread and the TUI's
refused-turn timer refreshes seconds later. Both used to skip every live turn, so a turn that started in between
was overwritten with a non-running outcome. The concluding turn now captures its own evidence when it concludes
(``capture_turn_identity``) and only that evidence is discounted.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import hermes_state_outcome as so
from gateway.turn_outcome import refresh_gateway_turn_outcome
from tests.hermes_state import test_outcome_turn_end_declaration as helpers

from tests.waiting import wait_until
from tui_gateway.turn_marker import clear_turn_marker, record_turn_start


db = helpers.db
KINDS = ["routing", "lease", "marker"]


class Turns:
    """Begin/end one kind of live-turn evidence for chat ``chat`` (a gateway routing token, a turn lease, a TUI marker)."""

    def __init__(self, db, kind, monkeypatch):
        self.db, self.kind = db, kind
        monkeypatch.setattr(so, "_gateway_running", lambda home: True)

    def begin(self, n):
        if self.kind == "routing":
            self.db.save_gateway_routing_entry("route", json.dumps({
                "session_id": "chat", "active_turn_token": f"tok-{n}",
                "active_turn_started_at": datetime.now(UTC).isoformat()}))
        elif self.kind == "lease":
            assert self.db.try_acquire_session_turn_lease("chat", f"turn-{n}", ttl_seconds=300.0)
        else:
            t0 = time.time()  # a new marker carries a new start time
            wait_until(lambda: time.time() > t0 + 0.02, timeout=2.0)
            assert record_turn_start(self.db.db_path.parent, "chat", f"prompt {n}")

    def end(self, n):
        if self.kind == "routing":
            self.db.replace_gateway_routing_entries({})
        elif self.kind == "lease":
            self.db.release_session_turn_lease("chat", f"turn-{n}")
        else:
            clear_turn_marker(self.db.db_path.parent, "chat")


def _gate_refresh(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real = so.refresh_outcome

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(so, "refresh_outcome", delayed)
    return entered, release


def _next_turn_starts(db):
    db.append_message("chat", role="user", content="start the next task", timestamp=time.time() + 1)


@pytest.mark.parametrize("kind", KINDS)
def test_a_late_gateway_refresh_keeps_the_newer_turn_running(db, monkeypatch, kind):
    turns = Turns(db, kind, monkeypatch)
    helpers._declare_then_reply(db)
    turns.begin(1)  # the concluding turn's evidence, not yet released
    entered, release = _gate_refresh(monkeypatch)
    thread = refresh_gateway_turn_outcome(SimpleNamespace(_session_db=db), SimpleNamespace(session_id="chat"))
    assert entered.wait(10)
    try:
        turns.end(1)
        _next_turn_starts(db)
        turns.begin(2)
        assert so.derive_outcome("chat", db=db)["kind"] == "running"
        release.set()
        thread.join(10)
        assert not thread.is_alive()
        stored = so.get_outcome("chat", db=db)
        assert stored["kind"] == "running", stored
        assert stored["declared"]["kind"] == "waiting"  # the declared wait rides through the newer turn
    finally:
        release.set()
        thread.join(10)
        turns.end(2)


@pytest.mark.parametrize("kind", KINDS)
def test_a_gateway_refresh_still_discounts_its_own_unreleased_turn(db, monkeypatch, kind):
    turns = Turns(db, kind, monkeypatch)
    helpers._declare_then_reply(db)
    turns.begin(1)
    try:
        thread = refresh_gateway_turn_outcome(SimpleNamespace(_session_db=db), SimpleNamespace(session_id="chat"))
        thread.join(10)
        stored = so.get_outcome("chat", db=db)
        assert (stored["kind"], stored["source"]) == ("waiting", "declared"), stored
    finally:
        turns.end(1)


@pytest.mark.parametrize("kind", KINDS)
def test_a_failed_capture_discounts_nothing(db, monkeypatch, kind):
    """No evidence captured = no bypass: a live turn is running, as before this PR."""
    turns = Turns(db, kind, monkeypatch)
    helpers._declare_then_reply(db)
    turns.begin(1)
    try:
        assert so.refresh_outcome("chat", "turn_end", db=db, concluding_turn=frozenset())["kind"] == "running"
        assert so.refresh_outcome("chat", "turn_end", db=db)["kind"] == "running"
        assert so.refresh_outcome("chat", "turn_end", db=db, concluding_turn=["marker:1:2"])["kind"] == "running"
    finally:
        turns.end(1)


def test_capture_turn_identity_names_each_kind_of_evidence(db, monkeypatch):
    for kind, prefix in (("routing", "gateway:"), ("lease", "lease:"), ("marker", "marker:")):
        turns = Turns(db, kind, monkeypatch)
        assert so.capture_turn_identity("chat", db=db) == frozenset()
        turns.begin(1)
        first = so.capture_turn_identity("chat", db=db)
        assert len(first) == 1 and next(iter(first)).startswith(prefix)
        turns.end(1)
        turns.begin(2)
        assert so.capture_turn_identity("chat", db=db).isdisjoint(first)  # a later turn has its own identity
        turns.end(2)


# ── TUI / auto-continue entry path ──────────────────────────────────────────


@pytest.fixture
def timers(monkeypatch):
    fired = []

    class _Timer:
        def __init__(self, delay, fn):
            fired.append(fn)

        daemon = False

        def start(self):
            pass

    monkeypatch.setattr(threading, "Timer", _Timer)
    return fired


def _arm_refused_retry(db, monkeypatch, timers):
    from tui_gateway import server

    monkeypatch.setattr(server, "_hermes_home", db.db_path.parent)
    monkeypatch.setattr(server, "_session_profile_runtime_scope", lambda s: contextlib.nullcontext(), raising=False)
    monkeypatch.setattr(server, "_session_db", lambda s: contextlib.nullcontext(db), raising=False)
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a: None)
    monkeypatch.setattr("tui_gateway.update_pause._detect_skew", lambda: None)
    session = {"session_key": "chat", "agent": None, "_refused_turn_delay": 45.0}
    server._schedule_refused_turn_retry("chat", session, "retry-marker-key")
    assert len(timers) == 1
    return timers[0]


@pytest.mark.parametrize("kind", ["routing", "lease"])
def test_the_refused_turn_timer_keeps_a_newer_turn_running(db, monkeypatch, timers, kind):
    turns = Turns(db, kind, monkeypatch)
    helpers._declare_then_reply(db)
    turns.begin(1)  # the refused turn, concluding
    fire = _arm_refused_retry(db, monkeypatch, timers)
    turns.end(1)
    _next_turn_starts(db)
    turns.begin(2)  # the user's next turn starts during the retry wait
    try:
        fire()
        stored = so.get_outcome("chat", db=db)
        assert stored["kind"] == "running", stored
        assert stored["declared"]["kind"] == "waiting"
    finally:
        turns.end(2)


@pytest.mark.parametrize("kind", ["routing", "lease"])
def test_the_refused_turn_timer_discounts_the_refused_turns_own_evidence(db, monkeypatch, timers, kind):
    turns = Turns(db, kind, monkeypatch)
    helpers._declare_then_reply(db)
    turns.begin(1)
    fire = _arm_refused_retry(db, monkeypatch, timers)
    try:
        fire()  # no newer turn: only the refused turn's own, still unreleased, evidence is present
        stored = so.get_outcome("chat", db=db)
        assert (stored["kind"], stored["source"]) == ("waiting", "declared"), stored
    finally:
        turns.end(1)


def test_the_tui_turn_end_captures_its_own_evidence_when_none_is_passed(db, monkeypatch):
    from tui_gateway import server
    from tui_gateway import turn_outcome as tui

    turns = Turns(db, "lease", monkeypatch)
    helpers._declare_then_reply(db)
    monkeypatch.setattr(server, "_session_profile_runtime_scope", lambda s: contextlib.nullcontext(), raising=False)
    monkeypatch.setattr(server, "_session_db", lambda s: contextlib.nullcontext(db), raising=False)
    turns.begin(1)
    try:
        tui.refresh_turn_outcome({"session_key": "chat"})
        stored = so.get_outcome("chat", db=db)
        assert (stored["kind"], stored["source"]) == ("waiting", "declared"), stored
        assert tui.capture_concluding_turn({"session_key": "chat"}) == so.capture_turn_identity("chat", db=db)
    finally:
        turns.end(1)
    assert tui.capture_concluding_turn({"session_key": ""}) == frozenset()

