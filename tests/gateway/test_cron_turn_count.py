"""``cron_turn_count``: the agent turns of cron runs, counted exactly where the work total counts them.

A ``cron_cut`` core update cuts cron runs; an agent cron's turn runs on the owner, so the total holds it
once as the scheduler run and once as a turn. Callers add this count to the cut cron work, so it must
never exceed what the total holds for those sessions (an overcount would waive unknown work).
"""
import threading
from types import SimpleNamespace

import pytest

from gateway import session_policy
from gateway.session_runtime_workers import cron_turn_count, uncounted_runtime_work


class _Task:
    def done(self):
        return False


def _live(sid, source, *, executing=True):
    stream = SimpleNamespace(execution={"execution_generation": 1} if executing else {})
    return SimpleNamespace(route=f"route-{sid}", source=source, task=_Task(), event_stream=stream)


def _worker(*, closed=False):
    event = threading.Event()
    if closed:
        event.set()
    return SimpleNamespace(closed=event)


@pytest.fixture
def policies(monkeypatch):
    """source -> frozen policy; a source mapped to an exception makes the policy unreadable."""
    table = {}

    def lookup(runner, source):
        value = table.get(source)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(session_policy, "policy_for_source", lookup)
    return table


def _runner(sessions, workers=None, running=()):
    authority = SimpleNamespace(sessions=sessions, _managed_workers=workers or {},
                                _mutation_tasks={}, _side_agents={})
    return SimpleNamespace(session_authority=authority, _running_agents={r: object() for r in running})


def test_in_process_cron_turn_counts_once(policies):
    policies["cron-src"] = SimpleNamespace(source="cron")
    policies["chat-src"] = SimpleNamespace(source="gui")
    runner = _runner({"c": _live("c", "cron-src"), "d": _live("d", "chat-src")})
    assert cron_turn_count(runner) == 1


def test_cron_turn_in_running_agents_or_managed_worker_counts_once(policies):
    policies["cron-src"] = SimpleNamespace(source="cron")
    policies["card-src"] = SimpleNamespace(source="kanban")
    sessions = {"a": _live("a", "cron-src"), "b": _live("b", "cron-src"), "k": _live("k", "card-src")}
    runner = _runner(sessions, workers={"b": _worker(), "k": _worker()}, running=["route-a"])
    assert cron_turn_count(runner) == 2


def test_never_counts_what_the_total_does_not(policies):
    """A closed worker still executing and outside ``_running_agents`` is in no bucket of the total;
    an idle cron session is not work either."""
    policies["cron-src"] = SimpleNamespace(source="cron")
    sessions = {"closed": _live("closed", "cron-src"), "idle": _live("idle", "cron-src", executing=False)}
    runner = _runner(sessions, workers={"closed": _worker(closed=True)})
    assert uncounted_runtime_work(runner) == 0
    assert cron_turn_count(runner) == 0


def test_unreadable_policy_is_not_a_cron_turn(policies):
    from hermes_state_runtime import RuntimeStoreError
    policies["broken"] = RuntimeStoreError("storage_unavailable")
    policies["cron-src"] = SimpleNamespace(source="cron")
    runner = _runner({"x": _live("x", "broken"), "c": _live("c", "cron-src")})
    assert cron_turn_count(runner) == 1
