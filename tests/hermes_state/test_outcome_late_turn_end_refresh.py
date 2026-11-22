"""A turn-end refresh discounts only the concluding turn's OWN live-turn evidence. [daveey]

PR1131 reviews:
- t_01a1ce099f1c7723 (a007a31d): the gateway runs its turn-end refresh on an unawaited thread and the TUI's
  refused-turn timer refreshes seconds later; both skipped every live turn, so a turn that started in between was
  overwritten with a non-running outcome.
- t_01a1d0ca50bdd065 (ce343627): capturing "every live evidence the chat holds" at conclusion still took a newer
  TUI turn's marker (the TUI captured after ``settle_prompt_state`` admitted it) and a concurrent Desktop turn's
  marker (the gateway captured it while that turn waited on the lease).

The concluding turn now names its evidence from handles only it holds (``turn_identity``: the marker it recorded,
read back at once; its lease holder; its gateway ``active_turn_token``). Anything else live is a live turn.
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
from gateway.turn_outcome import concluding_turn_identity as gateway_identity
from gateway.turn_outcome import refresh_gateway_turn_outcome
from tests.hermes_state import test_outcome_turn_end_declaration as helpers

from tests.waiting import wait_until
from tui_gateway.turn_marker import clear_turn_marker, read_turn_marker, record_turn_start

db = helpers.db
KINDS = ["routing", "lease", "marker"]


class Turns:
    """Begin/end one kind of live-turn evidence for chat ``chat`` (a gateway routing token, a turn lease, a TUI
    marker). ``begin`` returns the handle the turn that owns it holds (token, holder, marker read back)."""

    def __init__(self, db, kind, monkeypatch):
        self.db, self.kind = db, kind
        monkeypatch.setattr(so, "_gateway_running", lambda home: True)

    def begin(self, n):
        if self.kind == "routing":
            self.db.save_gateway_routing_entry("route", json.dumps({
                "session_id": "chat", "active_turn_token": f"tok-{n}",
                "active_turn_started_at": datetime.now(UTC).isoformat()}))
            return f"tok-{n}"
        if self.kind == "lease":
            assert self.db.try_acquire_session_turn_lease("chat", f"turn-{n}", ttl_seconds=300.0)
            return f"turn-{n}"
        t0 = time.time()  # a new marker carries a new start time
        wait_until(lambda: time.time() > t0 + 0.02, timeout=2.0)
        assert record_turn_start(self.db.db_path.parent, "chat", f"prompt {n}")
        return read_turn_marker(self.db.db_path.parent, "chat")

    def end(self, n):
        if self.kind == "routing":
            self.db.replace_gateway_routing_entries({})
        elif self.kind == "lease":
            self.db.release_session_turn_lease("chat", f"turn-{n}")
        else:
            clear_turn_marker(self.db.db_path.parent, "chat")

    def identity(self, handle):
        key = {"routing": "gateway_token", "lease": "lease_holder", "marker": "marker"}[self.kind]
        return so.turn_identity(**{key: handle})


@pytest.fixture(autouse=True)
def gateway_outcome_scope():
    from gateway.turn_outcome import run_outcome
    token = run_outcome.set({"session_id": "chat"})
    try:
        yield
    finally:
        run_outcome.reset(token)


def _gateway(db, *, token=None, holder=None):
    """A runner, its run's frozen receipt (the agent's lease), and the turn's event (it owns the routing token)."""
    agent = SimpleNamespace(_last_session_turn_lease_holder=holder)
    from gateway.turn_outcome import capture_run_outcome
    capture_run_outcome(agent)
    return SimpleNamespace(_session_db=db), SimpleNamespace(_gateway_active_turn_token=token)


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


def _tui_scope(monkeypatch, db):
    from tui_gateway import server

    monkeypatch.setattr(server, "_session_profile_runtime_scope", lambda s: contextlib.nullcontext(), raising=False)
    monkeypatch.setattr(server, "_session_db", lambda s: contextlib.nullcontext(db), raising=False)


# ── identity ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", KINDS)
def test_the_named_identity_is_exactly_what_the_store_reads_live(db, monkeypatch, kind):
    """``turn_identity`` built from the owner's handle matches the token derivation reads, and a later turn of
    the same kind has a different one."""
    turns = Turns(db, kind, monkeypatch)
    first = turns.identity(turns.begin(1))
    try:
        assert set(so._live_turn_tokens(db, "chat")) == set(first)
        assert not so._live_turn(db, "chat", first)
    finally:
        turns.end(1)
    second = turns.identity(turns.begin(2))
    try:
        assert second.isdisjoint(first)
        assert so._live_turn(db, "chat", first)
    finally:
        turns.end(2)


def test_turn_identity_names_nothing_without_handles():
    assert so.turn_identity() == frozenset()
    assert so.turn_identity(marker=None, lease_holder="", gateway_token=None) == frozenset()
    assert so.turn_identity(lease_holder="h", gateway_token="t") == {"lease:h", "gateway:t"}


# ── gateway entry path ───────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["routing", "lease"])
def test_a_late_gateway_refresh_keeps_the_newer_turn_running(db, monkeypatch, kind):
    """Red on a007a31d (blanket ignore_live_turn): the late refresh stored unresolved/interrupted."""
    turns = Turns(db, kind, monkeypatch)
    helpers._declare_then_reply(db)
    own = turns.begin(1)  # the concluding gateway turn's evidence, not yet released
    runner, event = _gateway(db, token=own if kind == "routing" else None, holder=own if kind == "lease" else None)
    entered, release = _gate_refresh(monkeypatch)
    thread = refresh_gateway_turn_outcome(runner, SimpleNamespace(session_id="chat"), event, "key")
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


@pytest.mark.parametrize("kind", ["routing", "lease"])
def test_a_gateway_refresh_still_discounts_its_own_unreleased_turn(db, monkeypatch, kind):
    turns = Turns(db, kind, monkeypatch)
    helpers._declare_then_reply(db)
    own = turns.begin(1)
    runner, event = _gateway(db, token=own if kind == "routing" else None, holder=own if kind == "lease" else None)
    try:
        thread = refresh_gateway_turn_outcome(runner, SimpleNamespace(session_id="chat"), event, "key")
        thread.join(10)
        stored = so.get_outcome("chat", db=db)
        assert (stored["kind"], stored["source"]) == ("waiting", "declared"), stored
    finally:
        turns.end(1)


def test_the_gateway_never_names_a_concurrent_desktop_turns_marker(db, monkeypatch):
    """Review t_01a1d0ca50bdd065 repro 2: a Desktop/TUI turn for the same chat records its marker and waits on the
    lease the gateway turn holds. The gateway names only its own lease; the late refresh sees the TUI turn live."""
    monkeypatch.setattr(so, "_gateway_running", lambda home: True)
    helpers._declare_then_reply(db)
    assert db.try_acquire_session_turn_lease("chat", "gateway-turn", ttl_seconds=300.0)
    db.append_message("chat", role="user", content="tui prompt", timestamp=time.time() + 1)
    record_turn_start(db.db_path.parent, "chat", "tui prompt")  # TUI turn admitted, waiting on the lease
    runner, event = _gateway(db, holder="gateway-turn")
    assert not any(t.startswith("marker:") for t in gateway_identity(event))
    entered, release = _gate_refresh(monkeypatch)
    thread = refresh_gateway_turn_outcome(runner, SimpleNamespace(session_id="chat"), event, "key")
    assert entered.wait(10)
    try:
        db.release_session_turn_lease("chat", "gateway-turn")  # gateway turn ends; TUI turn still live
        release.set()
        thread.join(10)
        stored = so.get_outcome("chat", db=db)
        assert stored["kind"] == "running", stored
    finally:
        release.set()
        thread.join(10)
        clear_turn_marker(db.db_path.parent, "chat")


def test_the_gateway_names_its_agents_lease_and_its_events_token(db):
    _, event = _gateway(db, token="tok-x", holder="pid=1:turn=a")
    assert gateway_identity(event) == {"gateway:tok-x", "lease:pid=1:turn=a"}
    from gateway.turn_outcome import run_outcome
    run_outcome.get().clear()  # no worker-owned receipt: nothing discounted
    assert gateway_identity(None) == frozenset()
    assert gateway_identity(SimpleNamespace(_gateway_active_turn_token="t")) == {"gateway:t"}


@pytest.mark.parametrize("kind", KINDS)
def test_a_failed_capture_discounts_nothing(db, monkeypatch, kind):
    """No evidence named = no bypass: a live turn is running, as before this PR."""
    turns = Turns(db, kind, monkeypatch)
    helpers._declare_then_reply(db)
    turns.begin(1)
    try:
        assert so.refresh_outcome("chat", "turn_end", db=db, concluding_turn=frozenset())["kind"] == "running"
        assert so.refresh_outcome("chat", "turn_end", db=db)["kind"] == "running"
        assert so.refresh_outcome("chat", "turn_end", db=db, concluding_turn=["marker:1:2"])["kind"] == "running"
        thread = refresh_gateway_turn_outcome(SimpleNamespace(_session_db=db), SimpleNamespace(session_id="chat"))
        thread.join(10)
        assert so.get_outcome("chat", db=db)["kind"] == "running"
    finally:
        turns.end(1)


# ── TUI turn-end entry path ──────────────────────────────────────────────────


def test_the_tui_turn_end_refresh_after_settle_keeps_a_newer_turn_running(db, monkeypatch):
    """Review t_01a1d0ca50bdd065 repro 1: prompt_turn.run_body settles (a new prompt can be admitted and record its
    marker under the same key) before its finally-path refresh. The concluding turn names the marker it recorded,
    not the newer one now on disk."""
    from tui_gateway import turn_outcome as tui

    _tui_scope(monkeypatch, db)
    helpers._declare_then_reply(db)
    turns = Turns(db, "marker", monkeypatch)
    own = turns.begin(1)  # the concluding turn's marker, read back when it was recorded
    turns.end(1)  # _retire_turn_marker
    _next_turn_starts(db)
    turns.begin(2)  # the newer turn admitted after settle
    agent = SimpleNamespace(session_id="chat", _last_session_turn_lease_holder="turn-1")
    try:
        assert so.derive_outcome("chat", db=db)["kind"] == "running"
        tui.refresh_turn_outcome({"session_key": "chat"}, agent, tui.concluding_turn_identity(agent, own))
        stored = so.get_outcome("chat", db=db)
        assert stored["kind"] == "running", stored
        tui.refresh_turn_outcome({"session_key": "chat"})  # nothing named: still running
        assert so.get_outcome("chat", db=db)["kind"] == "running"
    finally:
        turns.end(2)


@pytest.mark.parametrize("kind", ["marker", "lease"])
def test_the_tui_turn_end_discounts_its_own_unreleased_evidence(db, monkeypatch, kind):
    from tui_gateway import turn_outcome as tui

    _tui_scope(monkeypatch, db)
    helpers._declare_then_reply(db)
    turns = Turns(db, kind, monkeypatch)
    own = turns.begin(1)
    agent = SimpleNamespace(session_id="chat",
                            _last_session_turn_lease_holder=own if kind == "lease" else None)
    try:
        tui.refresh_turn_outcome({"session_key": "chat"}, agent,
                                 tui.concluding_turn_identity(agent, own if kind == "marker" else None))
        stored = so.get_outcome("chat", db=db)
        assert (stored["kind"], stored["source"]) == ("waiting", "declared"), stored
    finally:
        turns.end(1)


def test_own_turn_marker_reads_back_what_the_turn_recorded(db):
    from tui_gateway import turn_outcome as tui

    home = db.db_path.parent
    session = {"session_key": "chat", "profile_home": str(home)}
    assert tui.own_turn_marker(session, "") is None
    assert tui.own_turn_marker(session, "chat") is None
    record_turn_start(home, "chat", "prompt")
    try:
        own = tui.own_turn_marker(session, "chat")
        assert so.turn_identity(marker=own) == set(so._live_turn_tokens(db, "chat"))
    finally:
        clear_turn_marker(home, "chat")


# ── TUI refused-turn auto-continue timer entry path ──────────────────────────


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


def _arm_refused_retry(db, monkeypatch, timers, concluding):
    from tui_gateway import server

    monkeypatch.setattr(server, "_hermes_home", db.db_path.parent)
    _tui_scope(monkeypatch, db)
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a, **k: None)
    monkeypatch.setattr("tui_gateway.update_pause._detect_skew", lambda: None)
    session = {"session_key": "chat", "agent": None, "_refused_turn_delay": 45.0}
    server._schedule_refused_turn_retry("chat", session, "retry-marker-key", concluding)
    assert len(timers) == 1
    return timers[0]


@pytest.mark.parametrize("kind", KINDS)
def test_the_refused_turn_timer_keeps_a_newer_turn_running(db, monkeypatch, timers, kind):
    turns = Turns(db, kind, monkeypatch)
    helpers._declare_then_reply(db)
    own = turns.begin(1)  # the refused turn, concluding
    fire = _arm_refused_retry(db, monkeypatch, timers, turns.identity(own))
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


@pytest.mark.parametrize("kind", ["lease", "marker"])
def test_the_refused_turn_timer_discounts_the_refused_turns_own_evidence(db, monkeypatch, timers, kind):
    turns = Turns(db, kind, monkeypatch)
    helpers._declare_then_reply(db)
    own = turns.begin(1)
    fire = _arm_refused_retry(db, monkeypatch, timers, turns.identity(own))
    try:
        fire()  # no newer turn: only the refused turn's own, still unreleased, evidence is present
        stored = so.get_outcome("chat", db=db)
        assert (stored["kind"], stored["source"]) == ("waiting", "declared"), stored
    finally:
        turns.end(1)
