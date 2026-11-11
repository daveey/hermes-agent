"""A re-adopted orphan's exit reaches its chat even when the chat is not open yet. [daveey]

After a Desktop restart the new backend re-adopts a chat's ``notify_on_complete`` job
(tools/process_registry_readopt.py). Its exit completion is addressed to that chat's
session_key, but the first poller to dequeue it usually belongs to another chat.
Unowned addressed events are normally dropped; a re-adopted completion is parked
instead, and the owning chat's poller takes it once that chat is open.
"""

from __future__ import annotations

import threading

import pytest

from tools.process_registry import ProcessRegistry
from tools.process_registry_awaiting import release
from tui_gateway import server

KEY = "20261014_152627_e39461"
EVENT = {"type": "completion", "session_id": "proc_4fa0a87e0e10", "session_key": KEY, "command": "bash x.sh",
         "exit_code": None, "completion_reason": "unknown", "park_if_unowned": True}


@pytest.fixture
def owners(monkeypatch):
    owned: set[str] = set()
    monkeypatch.setattr(server, "_notification_event_belongs_elsewhere", lambda sid, session, evt: False)
    monkeypatch.setattr(server, "_session_owns_notification_event",
                        lambda sid, session, evt: sid in owned and evt.get("session_key") == KEY)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    return owned


def _session() -> dict:
    return {"history_lock": threading.RLock(), "running": False, "history": [], "agent": None, "profile_home": None}


def test_unowned_readopted_completion_is_parked_not_dropped(owners):
    registry = ProcessRegistry()
    completions: list = []
    assert server._notif_handle_event("other", _session(), dict(EVENT), set(), registry, lambda e: "t", None,
                                      completions) is True
    assert completions == []
    assert registry.completion_queue.empty()
    taken = registry.take_parked_events(lambda e: True)
    try:
        assert [e["session_id"] for e in taken] == ["proc_4fa0a87e0e10"]
    finally:
        release(taken)


def test_ordinary_unowned_completion_is_still_dropped(owners):
    registry = ProcessRegistry()
    evt = {k: v for k, v in EVENT.items() if k != "park_if_unowned"}
    server._notif_handle_event("other", _session(), evt, set(), registry, lambda e: "t", None, [])
    assert registry.take_parked_events(lambda e: True) == []


def test_owner_poller_takes_the_parked_completion(owners):
    registry = ProcessRegistry()
    registry.park_unowned_event(dict(EVENT))
    handled: list = []
    handle = lambda events, deferred: handled.extend(events)

    assert server._notif_take_parked("other", _session(), registry, handle) == 0
    assert handled == []

    owners.add("owner")
    assert server._notif_take_parked("owner", _session(), registry, handle) == 1
    assert [e["session_key"] for e in handled] == [KEY]
    assert server._notif_take_parked("owner", _session(), registry, handle) == 0, "taken once"


def test_owner_handles_parked_completion_as_a_turn(owners, monkeypatch):
    """End to end through the real handler: the owner batches it into a completion turn."""
    registry = ProcessRegistry()
    registry.park_unowned_event(dict(EVENT))
    owners.add("owner")
    completions: list = []
    handle = lambda events, deferred: [server._notif_handle_event(
        "owner", _session(), e, set(), registry, lambda evt: "wake", deferred, completions) for e in events]
    server._notif_take_parked("owner", _session(), registry, handle)
    assert [(e["session_id"], text) for e, text in completions] == [("proc_4fa0a87e0e10", "wake")]


# ── 18:31 PDT incident (session 20261014_142820_ff330a): the exit never reached the chat ──────────────────────
# At 18:29:00 the launchd ``hermes dashboard`` (not a Desktop backend: no chats, no pollers) ran the same
# web_server lifespan 3 s before the Desktop ``serve`` backend and won the checkpoint lock, so IT adopted the
# chat's job. The exit at 18:31:30 was queued in the dashboard's in-memory queue, where no chat ever runs. And
# even in the right backend, a chat that is not open had no poller to take the parked event until the user
# opened it by hand.


def _job(**over):
    from types import SimpleNamespace
    base = dict(id="proc_48f7d096bd6b", pid=94988, session_key="20261014_142820_ff330a", task_id="default",
                owner_task_id="", command="bash ~/.hermes/scripts/pdu_campaign_wake.sh", started_at=1.0)
    return SimpleNamespace(**{**base, **over})


def test_only_a_desktop_owned_backend_adopts_orphans(monkeypatch):
    """The dashboard must never adopt: nothing there can deliver the exit to a chat."""
    from tools import process_registry_readopt as readopt
    calls = []

    class _Reg:
        def recover_from_checkpoint(self, owner=None):
            calls.append(owner)
            return 0

    monkeypatch.setattr("hermes_cli.process_identity.is_desktop_owned_backend", lambda argv=None: False)
    assert readopt.recover_tui_orphaned_processes(_Reg()) == 0
    assert calls == [], "a non-Desktop backend (hermes dashboard) adopted a chat's job"

    monkeypatch.setattr("hermes_cli.process_identity.is_desktop_owned_backend", lambda argv=None: True)
    readopt.recover_tui_orphaned_processes(_Reg())
    assert calls == ["tui"]


def test_unknown_exit_goes_to_the_installed_sink_not_the_bare_queue():
    registry = ProcessRegistry()
    seen: list = []
    registry.on_unknown_exit = seen.append
    registry._queue_unknown_exit(_job())
    assert [e["session_key"] for e in seen] == ["20261014_142820_ff330a"]
    assert registry.completion_queue.empty()


def test_unknown_exit_without_a_sink_still_queues():
    registry = ProcessRegistry()
    registry._queue_unknown_exit(_job())
    assert registry.completion_queue.get_nowait()["session_id"] == "proc_48f7d096bd6b"


def test_exit_for_a_chat_that_is_not_open_wakes_it(monkeypatch):
    """Parked AND the chat is resumed in-process (like the startup-resume sweep), so its poller runs the turn."""
    registry = ProcessRegistry()
    resumed: list = []
    monkeypatch.setattr(server, "_notif_live_session_matches", lambda keys, exclude=None: False)
    monkeypatch.setitem(server._methods, "session.resume",
                        lambda rid, params: resumed.append(params) or {"result": {"session_id": "new-sid"}})
    t = server._readopted_exit_deliver(dict(EVENT), registry=registry)
    assert t is not None
    t.join(5)
    taken = registry.take_parked_events(lambda e: True)
    try:
        assert [e["session_key"] for e in taken] == [KEY]
    finally:
        release(taken)
    assert resumed and resumed[0]["session_id"] == KEY and resumed[0].get("omit_messages") is True


def test_exit_for_an_open_chat_is_parked_without_a_resume(monkeypatch):
    registry = ProcessRegistry()
    monkeypatch.setattr(server, "_notif_live_session_matches", lambda keys, exclude=None: KEY in keys)
    monkeypatch.setitem(server._methods, "session.resume", lambda rid, params: pytest.fail("resumed an open chat"))
    assert server._readopted_exit_deliver(dict(EVENT), registry=registry) is None
    taken = registry.take_parked_events(lambda e: True)
    try:
        assert len(taken) == 1
    finally:
        release(taken)


def test_wake_tries_named_profiles_when_the_launch_store_lacks_the_chat(monkeypatch, tmp_path):
    (tmp_path / "profiles" / "bb").mkdir(parents=True)
    (tmp_path / "profiles" / "bb" / "state.db").write_bytes(b"")
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    tried: list = []

    def resume(rid, params):
        tried.append(params.get("profile"))
        if params.get("profile") == "bb":
            return {"result": {"session_id": "x"}}
        return {"error": {"code": 4007, "message": "session not found"}}

    monkeypatch.setitem(server._methods, "session.resume", resume)
    assert server._wake_chat_for_readopted_exit(KEY) == "bb"
    assert tried == [None, "bb"]


def test_wake_stops_on_a_refusal_other_than_not_found(monkeypatch, tmp_path):
    (tmp_path / "profiles" / "bb").mkdir(parents=True)
    (tmp_path / "profiles" / "bb" / "state.db").write_bytes(b"")
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    tried: list = []
    monkeypatch.setitem(server._methods, "session.resume", lambda rid, params: tried.append(params.get("profile"))
                        or {"error": {"code": 4011, "message": "open in another Hermes window"}})
    assert server._wake_chat_for_readopted_exit(KEY) is None
    assert tried == [None]
