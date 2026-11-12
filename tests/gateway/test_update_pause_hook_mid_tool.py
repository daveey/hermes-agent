"""[daveey] The gateway hook's mid-tool cancel watcher (``gateway.session_update_pause_hook``), unit by unit.

Two rules the composed test does not pin: a pending Stop or steer beats the cancel (the boundary would not
pause then, so a cut call's stamped result would only mislead the turn that goes on), and leaving
``installed()`` stops the turn's watcher, so a later pause request never cancels a later turn's call.
"""
from __future__ import annotations

import time

import pytest

from agent import update_pause_cancel
from gateway import session_update_pause_hook as hook
from hermes_cli import admission_fence as af
from tests.waiting import wait_until
from tui_gateway import update_pause as up


class _Agent:
    def __init__(self):
        self._interrupt_requested = False
        self._pending_steer: str | None = None
        self.interrupts = []

    def interrupt(self, message=None, *, tool_reason=None, **_kw):
        self.interrupts.append(tool_reason)
        self._interrupt_requested = True


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "MID_TOOL_GRACE_S", 0.0)
    monkeypatch.setattr(up, "MID_TOOL_POLL_S", 0.02)
    root = hook.fence_root(tmp_path)
    yield root
    af.clear_hold(root)
    af.clear_pause_request(root)


def _request_pause(root):
    af.set_hold(root)
    af.request_pause(root, expires_at=time.time() + 60)


def _running(agent):
    update_pause_cancel.started(agent, "c1")
    return agent


def test_a_pending_steer_or_stop_is_never_cut_by_the_cancel(root):
    _request_pause(root)
    steered = _running(_Agent())
    steered._pending_steer = "also check the docs"  # the turn goes on with it: no pause at the boundary
    assert hook.mid_tool_cancel_step(steered, root, {}) is None
    assert steered.interrupts == [] and "_update_pause_cancel" not in vars(steered)

    stopped = _running(_Agent())
    stopped._interrupt_requested = True  # a user Stop is already pending: it wins
    assert hook.mid_tool_cancel_step(stopped, root, {}) is None and stopped.interrupts == []

    plain = _running(_Agent())  # control: the same request and running call are cancelled
    assert hook.mid_tool_cancel_step(plain, root, {}) == ["c1"]
    assert plain.interrupts == [update_pause_cancel.CANCEL_TOOL_REASON]


@pytest.mark.parametrize("raises", [False, True], ids=["turn-ended", "turn-raised"])
def test_leaving_installed_stops_the_turns_watcher(root, tmp_path, raises):
    live = _running(_Agent())
    with hook.bind_scope(tmp_path, eligible=True), hook.installed(live) as on:
        assert on
        _request_pause(root)
        wait_until(lambda: live.interrupts, timeout=5)  # control: the watcher runs while installed
    assert live.interrupts == [update_pause_cancel.CANCEL_TOOL_REASON]
    af.clear_pause_request(root)

    ended = _running(_Agent())
    try:
        with hook.bind_scope(tmp_path, eligible=True), hook.installed(ended):
            if raises:
                raise RuntimeError("turn failed")
    except RuntimeError:
        pass
    _request_pause(root)  # requested after the turn left the hook, its call still looking in flight
    time.sleep(0.3)  # health: allow HX013 -- a stopped watcher must stay silent for many polls past its grace
    assert ended.interrupts == []
