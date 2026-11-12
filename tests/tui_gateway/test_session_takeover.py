"""Cross-process "Take over here": the revocation watcher and the ``session.take_over`` RPC.

The displaced owner is modelled as a REAL live process (``sleep``) recorded in the registry, so the
liveness checks the production code relies on are exercised, not stubbed.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import types
from pathlib import Path

import pytest

from hermes_cli import active_sessions
from hermes_cli.active_sessions import SESSION_NOT_OWNED, acknowledge_takeover, take_over_active_session
from tui_gateway import server, server_requests

SID = "takeover-sid"
KEY = "takeover-session-key"


def _session(**extra) -> dict:
    return {
        "agent": None, "session_key": KEY, "history": [], "history_lock": threading.Lock(),
        "history_version": 0, "running": False, "attached_images": [], "image_counter": 0, "cols": 80,
        "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all", "source": "desktop",
        **extra}


def _registry_path() -> Path:
    return Path(os.environ["HERMES_HOME"]) / "runtime" / "active_sessions.json"


def _entries() -> list[dict]:
    return json.loads(_registry_path().read_text())["entries"]


def _write_entries(entries: list[dict]) -> None:
    _registry_path().parent.mkdir(parents=True, exist_ok=True)
    _registry_path().write_text(json.dumps({"entries": entries}))


@pytest.fixture
def sleeper():
    procs = []

    def spawn():
        proc = subprocess.Popen(["sleep", "60"])
        procs.append(proc)
        return proc
    yield spawn
    for proc in procs:
        proc.kill()
        proc.wait()


def _foreign_entry(proc, *, lease_id="foreign-lease", took_over_from=None, live="other-window") -> dict:
    metadata = {"live_session_id": live}
    if took_over_from:
        metadata.update(took_over_from=list(took_over_from), took_over_at=1.0)
    return {"lease_id": lease_id, "session_id": KEY, "surface": "desktop", "pid": proc.pid,
            "process_start_time": active_sessions._process_start_time(proc.pid), "started_at": 1000.0,
            "updated_at": 1000.0, "track_liveness": True, "metadata": metadata}


@pytest.fixture
def env(monkeypatch):
    emitted: list[tuple] = []
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: emitted.append((event, sid, payload)) or True)
    server._takeover_watch_stamps.clear()
    sessions: list[dict] = []
    yield types.SimpleNamespace(emitted=emitted, sessions=sessions)
    for session in sessions:
        server._release_active_session_slot(session)
    server._sessions.pop(SID, None)
    server_requests.reset_for_tests()


def _own_session(env) -> tuple[dict, object]:
    session = _session()
    env.sessions.append(session)
    server._sessions[SID] = session
    assert server._ensure_active_session_slot(SID, session) is None
    lease = session["active_session_lease"]
    assert lease is not None and not lease.released
    return session, lease


def _foreign_takeover_of(lease, proc) -> None:
    """What another process's take_over_active_session leaves behind: only ITS entry, naming ours."""
    _write_entries([_foreign_entry(proc, lease_id="new-owner-lease", took_over_from=[lease.lease_id])])


# ── (a) idle owned session ──────────────────────────────────────────────────────────────────────


def test_watcher_revokes_an_idle_session_taken_over_by_another_process(env, sleeper):
    session, lease = _own_session(env)
    assert server._takeover_watch_pass() == 0  # nothing yet

    _foreign_takeover_of(lease, sleeper())
    assert server._takeover_watch_pass() == 1

    assert lease.released is True
    assert "active_session_lease" not in session
    assert session["_lease_taken_over"] is True
    errors = [p for e, s, p in env.emitted if e == "error" and s == SID]
    assert [e["error_surface"] for e in errors] == [
        {"layer": "gateway", "code": "SESSION_NOT_OWNED", "retryable": False}]
    first, details = errors[0]["message"].split("\n")
    assert first == "This chat was taken over by another Hermes window."
    # The Details line names the taker so the displaced window's card can say where the chat went.
    assert details.startswith("Details: session takeover-session-key opened by desktop")
    (entry,) = _entries()
    assert entry["lease_id"] == "new-owner-lease"  # the new owner's entry is untouched
    assert entry["metadata"]["handoff_acked"] == [lease.lease_id]

    refusal = server._ensure_active_session_slot(SID, session)
    assert getattr(refusal, "reason", None) == SESSION_NOT_OWNED

    # A second pass is a no-op (nothing held any more).
    assert server._takeover_watch_pass() == 0


def test_watcher_skips_the_read_when_the_registry_did_not_change(env, sleeper, monkeypatch):
    _session_obj, lease = _own_session(env)
    assert server._takeover_watch_pass() == 0
    reads = []
    real = active_sessions.find_takeover_of
    monkeypatch.setattr(active_sessions, "find_takeover_of", lambda *a, **k: reads.append(a) or real(*a, **k))
    assert server._takeover_watch_pass() == 0
    assert reads == []
    _foreign_takeover_of(lease, sleeper())
    assert server._takeover_watch_pass() == 1
    assert len(reads) == 1


def test_watcher_ignores_unrelated_registry_changes(env, sleeper):
    session, lease = _own_session(env)
    entries = _entries()
    _write_entries([*entries, {**_foreign_entry(sleeper()), "session_id": "some-other-chat"}])
    assert server._takeover_watch_pass() == 0
    assert session["active_session_lease"] is lease and not lease.released
    assert env.emitted == []


# ── (b) running turn ────────────────────────────────────────────────────────────────────────────


def test_watcher_interrupts_a_running_turn(env, sleeper):
    session, lease = _own_session(env)
    calls = []
    session["agent"] = types.SimpleNamespace(hard_interrupt=lambda *a, **k: calls.append("hard_interrupt"))
    session["running"] = True
    _foreign_takeover_of(lease, sleeper())

    assert server._takeover_watch_pass() == 1
    assert calls == ["hard_interrupt"]
    assert session["_turn_cancel_requested"] is True
    assert session["running"] is False  # no live run thread: the interrupt settles it


# ── (c) pending clarify ─────────────────────────────────────────────────────────────────────────


def test_watcher_cancels_a_pending_clarify(env, sleeper):
    _session_obj, lease = _own_session(env)
    req = server_requests.ServerRequest(
        SID, "clarify", {"questions": [{"qid": "q1", "question": "which one?"}]}, qids=["q1"])
    server_requests._register(req)
    assert server_requests.open_requests(SID)
    # The agent thread parked on the answer, as the clarify tool does.
    unblocked = threading.Event()
    waiter = threading.Thread(target=lambda: req.event.wait(30) and unblocked.set())
    waiter.start()
    _foreign_takeover_of(lease, sleeper())

    assert server._takeover_watch_pass() == 1
    waiter.join(5)
    assert unblocked.is_set()
    assert req.result == {"answers": {}, "outcome": "cancelled", "reason": "taken_over"}
    assert server_requests.open_requests(SID) == []


# ── (d) session.take_over RPC ───────────────────────────────────────────────────────────────────


@pytest.fixture
def rpc():
    """Call the handler synchronously (the dispatcher would run it on the RPC pool) and return its response."""
    handler = server._methods["session.take_over"]
    return lambda: handler("t", {"session_id": SID})


def _unowned_session(env) -> dict:
    session = _session()
    env.sessions.append(session)
    server._sessions[SID] = session
    return session


def test_take_over_acknowledged_when_the_old_owner_acks(env, sleeper, rpc, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_TAKEOVER_ACK_TIMEOUT_S", "10")
    proc = sleeper()
    _write_entries([_foreign_entry(proc)])
    session = _unowned_session(env)
    assert getattr(server._ensure_active_session_slot(SID, session), "reason", None) == SESSION_NOT_OWNED

    # The old owner: acks as soon as it sees the takeover (what its watcher does).
    def old_owner():
        import time
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if active_sessions.find_takeover_of("foreign-lease") is not None:
                acknowledge_takeover("foreign-lease")
                return
            time.sleep(0.05)
    t = threading.Thread(target=old_owner)
    t.start()
    resp = rpc()
    t.join()

    assert resp["result"]["status"] == "taken_over"
    assert resp["result"]["acknowledged"] is True
    assert resp["result"]["previous_owner"] == {"surface": "desktop", "pid": proc.pid, "started_at": 1000.0}
    lease = session["active_session_lease"]
    (entry,) = _entries()
    assert entry["lease_id"] == lease.lease_id and entry["pid"] == os.getpid()
    assert entry["metadata"]["took_over_from"] == ["foreign-lease"]
    assert server._ensure_active_session_slot(SID, session) is None  # we own it now


def test_take_over_not_acknowledged_after_the_timeout(env, sleeper, rpc, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_TAKEOVER_ACK_TIMEOUT_S", "0.3")
    _write_entries([_foreign_entry(sleeper())])
    session = _unowned_session(env)
    resp = rpc()
    assert resp["result"]["status"] == "taken_over"
    assert resp["result"]["acknowledged"] is False
    assert session["active_session_lease"] is not None


def test_take_over_reloads_history_from_the_store(env, sleeper, rpc, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_TAKEOVER_ACK_TIMEOUT_S", "0")
    _write_entries([_foreign_entry(sleeper())])
    session = _unowned_session(env)
    stored = [{"role": "user", "content": "hi", "_row_id": 1},
              {"role": "assistant", "content": "written by the other window", "_row_id": 2}]
    monkeypatch.setattr(server, "_load_durable_truncation_history", lambda s, **k: list(stored))
    rpc()
    assert [m["content"] for m in session["history"]] == ["hi", "written by the other window"]
    assert session["history_version"] == 1


def test_take_over_already_owner(env, rpc):
    session, lease = _own_session(env)
    resp = rpc()
    assert resp["result"] == {"status": "already_owner", "previous_owner": None, "acknowledged": True}
    assert session["active_session_lease"] is lease


def test_take_over_refused_when_the_registry_is_unreadable(env, rpc):
    _registry_path().parent.mkdir(parents=True, exist_ok=True)
    _registry_path().write_text("{not json")
    session = _unowned_session(env)
    resp = rpc()
    assert resp["error"]["code"] == 4090
    assert resp["error"]["data"] == {"reason": "SESSION_COORDINATION_UNAVAILABLE"}
    assert session.get("active_session_lease") is None
    assert _registry_path().read_text() == "{not json"


def test_take_over_unknown_session(env):
    resp = server._methods["session.take_over"]("t", {"session_id": "nope"})
    assert resp["error"]["code"] == 4001


def test_take_over_is_a_declared_long_handler():
    assert "session.take_over" in server._LONG_HANDLERS


def test_same_process_sibling_is_revoked_by_the_takeover(env, rpc, monkeypatch):
    """Another live runtime of the same chat in THIS process (a second window with a live client)."""
    monkeypatch.setenv("HERMES_SESSION_TAKEOVER_ACK_TIMEOUT_S", "2")
    sibling = _session()
    env.sessions.append(sibling)
    server._sessions["sibling-sid"] = sibling
    try:
        assert server._ensure_active_session_slot("sibling-sid", sibling) is None
        old_lease = sibling["active_session_lease"]
        monkeypatch.setattr(server, "_transport_is_dead", lambda t: False)  # the sibling still has a client
        session = _unowned_session(env)
        assert getattr(server._ensure_active_session_slot(SID, session), "reason", None) == SESSION_NOT_OWNED
        resp = rpc()
        assert resp["result"]["status"] == "taken_over" and resp["result"]["acknowledged"] is True
        assert old_lease.released and sibling["_lease_taken_over"] is True
        assert ("error", "sibling-sid") in [(e, s) for e, s, _ in env.emitted]
    finally:
        server._sessions.pop("sibling-sid", None)


def test_take_over_with_take_over_active_session_contract(env, sleeper):
    """Direct helper call mirrors what the RPC does for a foreign owner (sanity for the e2e)."""
    _write_entries([_foreign_entry(sleeper())])
    lease, removed, refusal = take_over_active_session(
        session_id=KEY, surface="desktop", metadata={"live_session_id": SID}, track_liveness=True)
    assert refusal is None and [e["lease_id"] for e in removed] == ["foreign-lease"]
    lease.release()


# ── (e) durable writer handoff: a live-but-slow writer ──────────────────────────────────────────
# Chat 20261112_052539_8d8534: a GIL-starved Desktop backend let go 22.8 s after Take over, but the
# fixed 8 s ACK deadline had already refused it twice. A writer provably alive here (same pid
# namespace, pid alive) is waited out up to HERMES_SESSION_TAKEOVER_LIVE_WRITER_WAIT_S.


@pytest.fixture
def durable_writer(env):
    """A Desktop session on a real state.db whose turn lease another writer holds."""
    from hermes_state import SessionDB

    home = Path(os.environ["HERMES_HOME"])
    db = SessionDB(home / "state.db")
    db.create_session(KEY, source="desktop")
    session = _session(profile_home=str(home))
    env.sessions.append(session)
    server._sessions[SID] = session
    held: list[str] = []
    timers: list[threading.Timer] = []

    def hold(holder: str) -> None:
        assert db.try_acquire_session_turn_lease(KEY, holder)
        held.append(holder)

    def release_after(holder: str, delay_s: float) -> threading.Timer:
        timer = threading.Timer(delay_s, db.release_session_turn_lease, args=(KEY, holder))
        timers.append(timer)
        timer.start()
        return timer

    yield types.SimpleNamespace(db=db, session=session, hold=hold, release_after=release_after)
    for timer in timers:
        timer.cancel()
        timer.join(5)
    for holder in held:
        db.release_session_turn_lease(KEY, holder)
    db.close()


def _same_ns_holder(pid: int, platform: str = "desktop") -> str:
    from hermes_state_pidns import holder_namespace_token
    token = holder_namespace_token()
    if not token:
        pytest.skip("needs a Linux pid namespace")
    return f"pid={pid}{token}:turn=slow:platform={platform}"


def test_take_over_waits_out_a_live_writer_that_releases_after_the_ack_deadline(
        durable_writer, sleeper, rpc, monkeypatch):
    import time
    monkeypatch.setenv("HERMES_SESSION_TAKEOVER_ACK_TIMEOUT_S", "0.3")
    monkeypatch.setenv("HERMES_SESSION_TAKEOVER_LIVE_WRITER_WAIT_S", "10")
    proc = sleeper()
    holder = _same_ns_holder(proc.pid)
    durable_writer.hold(holder)
    releaser = durable_writer.release_after(holder, 1.2)  # well past the 0.3 s ACK deadline
    started = time.monotonic()
    resp = rpc()
    elapsed = time.monotonic() - started
    releaser.join(5)
    assert resp.get("result", {}).get("status") == "taken_over", resp
    assert resp["result"]["acknowledged"] is True
    assert resp["result"]["previous_owner"]["pid"] == proc.pid
    assert 1.0 <= elapsed < 8
    assert durable_writer.db.session_turn_lease_owner(KEY) is None


def test_take_over_refuses_an_unprovable_writer_at_the_ack_deadline(durable_writer, rpc, monkeypatch):
    import time
    monkeypatch.setenv("HERMES_SESSION_TAKEOVER_ACK_TIMEOUT_S", "0.3")
    monkeypatch.setenv("HERMES_SESSION_TAKEOVER_LIVE_WRITER_WAIT_S", "10")
    gone = subprocess.Popen(["true"])
    gone.wait()
    # A dead pid this process cannot probe (foreign namespace stamp): the lease defers to its TTL and
    # nothing proves the writer alive, so the ACK deadline applies as before.
    holder = f"pid={gone.pid}:pidns=1:turn=gone:platform=desktop"
    durable_writer.hold(holder)
    started = time.monotonic()
    resp = rpc()
    elapsed = time.monotonic() - started
    assert resp["error"]["code"] == 4090
    assert resp["error"]["data"] == {"reason": "SESSION_NOT_OWNED"}
    assert 0.3 <= elapsed < 3
    message = resp["error"]["message"]
    assert "alive" not in message
    assert "older Hermes CLI" not in message  # the holder is a Desktop backend, not a CLI
    assert message.endswith("No writer was forced out.")
    assert durable_writer.db.session_turn_lease_owner(KEY)["holder"] == holder


def test_take_over_refuses_a_live_writer_that_never_releases_at_the_live_cap(
        durable_writer, sleeper, rpc, monkeypatch, caplog):
    import logging
    import time
    monkeypatch.setenv("HERMES_SESSION_TAKEOVER_ACK_TIMEOUT_S", "0.2")
    monkeypatch.setenv("HERMES_SESSION_TAKEOVER_LIVE_WRITER_WAIT_S", "1")
    proc = sleeper()
    holder = _same_ns_holder(proc.pid)
    durable_writer.hold(holder)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        resp = rpc()
    elapsed = time.monotonic() - started
    assert resp["error"]["code"] == 4090
    assert resp["error"]["data"] == {"reason": "SESSION_NOT_OWNED"}
    assert 1.0 <= elapsed < 5
    assert resp["error"]["message"] == (
        f"The Hermes desktop process (pid {proc.pid}) running this chat is alive but did not let go within 1 s; "
        "it may be overloaded. Retry Take over here in a minute. No writer was forced out.")
    assert any("session.take_over: writer" in r.getMessage() and "alive=True" in r.getMessage()
               for r in caplog.records)
    assert proc.poll() is None  # never forced out
    assert durable_writer.db.session_turn_lease_owner(KEY)["holder"] == holder


def test_take_over_refuses_a_same_namespace_writer_it_may_not_signal_at_the_ack_deadline(
        durable_writer, sleeper, rpc, monkeypatch):
    """kill(pid, 0) answering EPERM (another user's process) does not prove the writer alive."""
    import time
    monkeypatch.setenv("HERMES_SESSION_TAKEOVER_ACK_TIMEOUT_S", "0.3")
    monkeypatch.setenv("HERMES_SESSION_TAKEOVER_LIVE_WRITER_WAIT_S", "10")
    proc = sleeper()
    holder = _same_ns_holder(proc.pid)
    durable_writer.hold(holder)
    real_kill = os.kill
    probed: list[int] = []

    def kill(pid, sig):
        if pid == proc.pid and sig == 0:
            probed.append(pid)
            raise PermissionError(1, "Operation not permitted")
        return real_kill(pid, sig)

    monkeypatch.setattr(os, "kill", kill)
    started = time.monotonic()
    resp = rpc()
    elapsed = time.monotonic() - started
    monkeypatch.setattr(os, "kill", real_kill)
    assert probed  # the probe really ran and was refused
    assert resp["error"]["code"] == 4090, resp
    assert resp["error"]["data"] == {"reason": "SESSION_NOT_OWNED"}
    assert 0.3 <= elapsed < 3  # the 0.3 s ACK deadline, not the 10 s live-writer cap
    assert "alive" not in resp["error"]["message"]
    assert resp["error"]["message"].endswith("No writer was forced out.")
    assert durable_writer.db.session_turn_lease_owner(KEY)["holder"] == holder


@pytest.mark.parametrize(("why", "label"), [("lease_takeover", "taken_over"), ("backend_exit", "shutdown"),
                                            ("session_close", "session_closed"), (None, "interrupted")])
def test_interrupting_a_turn_names_why_in_the_pending_clarify_result(env, why, label):
    """A turn stop used to cancel its parked clarify as ``interrupted`` whatever the producer, so a takeover read
    like a plain Stop in the stored result. The label rides the result; no answer content does."""
    req = server_requests.ServerRequest(
        SID, "clarify", {"questions": [{"qid": "q1", "question": "which one?"}]}, qids=["q1"])
    server_requests._register(req)
    server._interrupt_session_turn(SID, _session(), reason=why)
    assert req.result == {"answers": {}, "outcome": "cancelled", "reason": label}
