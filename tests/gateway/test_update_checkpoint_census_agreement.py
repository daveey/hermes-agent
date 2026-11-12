"""The ``active-work`` census and the instant-update checkpoint agree on what the checkpoint preserves.

Synthetic accounting scenario consistent with hermes-hub's 2026-11-12 18:11Z logs: the census read
``active=7 units=7 (cron,managed)``; nine seconds later a checkpoint refused with
``preservable managed=3, cron=3`` and a named chat. Those aggregate snapshots do not prove continuity
of the seventh unit. This fixture demonstrates one possible representation gap: an executing session
outside ``_running_agents`` is listed as sessionless ``managed`` but is not a preservable card.
Mapping that synthetic session then makes it a named chat; this is not a historical identity claim.

Real ``GatewayRunner`` accounting (``_describe_active_work``, ``_active_work_count``, the legacy
``_running_agents`` view, ``uncounted_runtime_work``, ``kanban_managed_count``, ``cron_turn_count``) over
stub authorities; only the cron scheduler's registry and the session policy lookup are faked.
"""
import json
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway import session_policy
from gateway.run import GatewayRunner
from hermes_cli import admission_fence

CRON_JOBS = ("59ba0ea8ec4f", "1676d7b6a28c", "353210256a77")


class _Task:
    def done(self):
        return False


def _live(route, source):
    return SimpleNamespace(route=route, source=source, task=_Task(),
                           event_stream=SimpleNamespace(execution={"execution_generation": 1}))


def _card_worker(pid):
    return SimpleNamespace(kanban_card=True, closed=threading.Event(), worker=(pid,))


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """Synthetic gateway: 3 Kanban card workers, 3 cron runs, 1 executing turn not yet mapped."""
    from cron import scheduler
    from gateway import update_checkpoint as checkpoint
    monkeypatch.setattr(session_policy, "policy_for_source",
                        lambda runner, source: SimpleNamespace(source=source))
    monkeypatch.setattr(scheduler, "get_running_job_ids", lambda: frozenset(CRON_JOBS))
    monkeypatch.setattr(scheduler, "get_wedged_job_ids", lambda: frozenset())
    monkeypatch.setattr(scheduler, "get_running_job_details", lambda: [
        {"job_id": job, "elapsed_s": 60.0, "worker_pid": None, "restart_safe": False} for job in CRON_JOBS])
    monkeypatch.setattr("gateway.run_active_work_verb._delegation_units", lambda now: [])

    sessions = {f"local-card{i}": _live(f"agent:main:kanban:{i}", "kanban") for i in range(3)}
    sessions["local-dcf9"] = _live("agent:main:local:dm:local-dcf9", "gui")
    authority = SimpleNamespace(sessions=sessions, _mutation_tasks=set(), _side_agents={},
                                _managed_workers={f"local-card{i}": _card_worker(1000 + i) for i in range(3)})
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner.session_authorities = [authority]
    runner.config = object()
    runner.request_restart = Mock(return_value=True)

    home = tmp_path / "home"
    (home / "core-update").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(checkpoint, "get_hermes_home", lambda: home)
    monkeypatch.setattr(checkpoint, "get_default_hermes_root", lambda **kw: home)
    monkeypatch.setattr(checkpoint, "known_cron_reservations", lambda: set())
    monkeypatch.setattr(checkpoint, "preserve_cron_slots", Mock())
    admission_fence.set_hold(home)
    (home / "core-update/intent").write_text(json.dumps(
        {"pid": os.getpid(), "kind": "unfenced", "checkpoint": True, "cron_cut": True,
         "created_at": time.time(), "expires_at": time.time() + 120, "nonce": "episode-one"}))
    return SimpleNamespace(runner=runner, checkpoint=checkpoint)


def census(runner):
    from gateway.run_active_work_verb import active_work_verb
    return active_work_verb(runner, SimpleNamespace(call_soon_threadsafe=lambda fn: fn()))({})


def ask_checkpoint(hub):
    return hub.checkpoint.checkpoint_for_update(
        hub.runner, SimpleNamespace(call_soon_threadsafe=lambda fn: fn()), {"nonce": "episode-one"})


def test_the_census_at_the_cap_reports_the_unit_the_checkpoint_refuses(hub):
    answer = census(hub.runner)
    kinds = sorted({unit["kind"] for unit in answer["active_work"]})
    # The same aggregate shape as the log: kinds alone show nothing the checkpoint refuses...
    assert answer["active_agents"] == len(answer["active_work"]) == 7 and kinds == ["cron", "managed"]
    # ...but the census now carries the checkpoint's own verdict on the same work.
    assert answer["checkpoint"] == {"active": 7, "preservable_managed": 3, "cron": 3,
                                    "unlisted": 1, "preservable": False}
    refused = ask_checkpoint(hub)
    assert refused["busy"] and not refused.get("pausing")
    hub.runner.request_restart.assert_not_called()
    assert refused["reason"].startswith("unpreservable work: 1 other unit (an unmapped turn, a side agent or a "
                                        "managed turn that is not a Kanban card) (active=7, preservable "
                                        "managed=3, cron=3)")


def test_once_mapped_the_same_turn_is_a_named_chat_turn_in_both(hub):
    """A possible mapping transition: census and refusal name the synthetic session's chat turn.
    The live ``_running_agents`` is a SessionFieldView, not a dict."""
    hub.runner._running_agents["agent:main:local:dm:local-dcf9"] = SimpleNamespace()
    answer = census(hub.runner)
    assert [u["session"] for u in answer["active_work"] if u["kind"] == "chat"] == ["agent:main:local:dm:local-dcf9"]
    assert answer["checkpoint"]["preservable"] is False and answer["active_agents"] == 7
    assert ask_checkpoint(hub)["reason"].startswith(
        "unpreservable work: 1 chat turn (agent:main:local:dm:local-dcf9) (active=7, preservable managed=3, cron=3)")


def test_cards_and_cut_cron_alone_are_preservable_in_both(hub):
    authority = hub.runner.session_authorities[0]
    del authority.sessions["local-dcf9"]
    answer = census(hub.runner)
    assert answer["checkpoint"] == {"active": 6, "preservable_managed": 3, "cron": 3,
                                    "unlisted": 0, "preservable": True}
    assert ask_checkpoint(hub)["pausing"] is True


def test_a_managed_turn_that_is_not_a_card_is_unpreservable_in_both(hub):
    authority = hub.runner.session_authorities[0]
    del authority.sessions["local-dcf9"]
    authority._managed_workers["local-card0"].kanban_card = False  # a managed Desktop turn
    answer = census(hub.runner)
    assert {u["kind"] for u in answer["active_work"]} == {"cron", "managed"}
    assert answer["checkpoint"]["preservable"] is False
    assert "1 other unit" in ask_checkpoint(hub)["reason"]


@pytest.mark.parametrize("mapped", [False, True])
def test_cut_cron_turn_does_not_mask_hidden_noncard(hub, monkeypatch, mapped):
    from cron import scheduler
    authority = hub.runner.session_authorities[0]
    authority.sessions.clear()
    authority._managed_workers.clear()
    authority.sessions["cron_j"] = _live("cron_j", "cron")
    authority.sessions["hidden"] = _live("hidden", "gui")
    authority._managed_workers["hidden"] = _card_worker(1001)
    authority._managed_workers["hidden"].kanban_card = False
    monkeypatch.setattr(scheduler, "get_running_job_ids", lambda: frozenset({"j"}))
    monkeypatch.setattr(scheduler, "get_running_job_details", lambda: [
        {"job_id": "j", "elapsed_s": 60.0, "worker_pid": None, "restart_safe": False}])
    if mapped:
        hub.runner._running_agents["cron_j"] = SimpleNamespace()
    answer = census(hub.runner)
    assert answer["checkpoint"]["active"] == 3
    assert answer["checkpoint"]["cron"] == 2
    refused = ask_checkpoint(hub)
    assert refused["busy"] and "1 other unit" in refused["reason"]
    assert answer["checkpoint"]["unlisted"] == 1
    hub.runner.request_restart.assert_not_called()
    hub.checkpoint.preserve_cron_slots.assert_not_called()
    # Without cron cutting, the mapped chat remains named but the cron job is also unlisted.
    uncut = hub.checkpoint.checkpoint_work(hub.runner, cron_cut=False)
    assert uncut["unlisted"] == (2 if mapped else 3)
    assert uncut["preservable"] is False


def test_an_unreadable_verdict_is_omitted_not_reported_preservable(hub, monkeypatch):
    monkeypatch.setattr(hub.checkpoint, "kanban_managed_count", Mock(side_effect=RuntimeError("boom")))
    answer = census(hub.runner)
    assert "checkpoint" not in answer and len(answer["active_work"]) == 7


def test_a_profile_scoped_answer_carries_no_whole_gateway_verdict(hub):
    from gateway.run_active_work_verb import active_work_verb
    hub.runner._served_profile_homes = {}
    answer = active_work_verb(hub.runner, SimpleNamespace(call_soon_threadsafe=lambda fn: fn()))({"profile": "x"})
    assert "checkpoint" not in answer
