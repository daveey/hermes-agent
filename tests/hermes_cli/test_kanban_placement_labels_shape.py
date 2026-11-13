"""Malformed Board registry ``labels`` fail closed in Kanban placement liveness. [daveey]

PR #2171 review nit (t_01a1d4be7c79b6e4, GitHub review 5890499417; fixed in t_01a1d4c327277a9d):
``label_machines`` evaluated ``wanted <= set(entry.get("labels") or [])``. A non-iterable ``labels`` (an int) raised
TypeError and a str matched its characters (``"x"`` carried label ``x``). Since #2171 the named-machine branch of
``_machine_picks_up`` also goes through ``label_machines`` (empty label set), so one malformed entry crashed named
routing too. Like ``kanban_placement_coordination._carries``, labels must be a list of str; a missing or null list is
empty; any other shape does not qualify.
"""
from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_worker_node as wn


def _registry(monkeypatch, entry):
    entry = {"state": "active", "last_seen": time.time() * 1000, **entry}
    monkeypatch.setattr(wn, "registry", lambda: {"me": "hub", "machines": {"worker-1": entry}})


@pytest.mark.parametrize("labels", [5, "x", "pool:worker", {"pool:worker": 1}, ("pool:worker",), ["pool:worker", 5]])
def test_malformed_registry_labels_fail_closed(monkeypatch, labels):
    _registry(monkeypatch, {"labels": labels})
    assert kp.label_machines("x") == []
    assert kp.label_machines("pool:worker") == []
    assert kp.label_machines(()) == []
    assert kp._machine_picks_up("pool:worker") is False
    assert kp._machine_picks_up(("pool:worker",)) is False
    assert kp._machine_picks_up("worker-1") is False


@pytest.mark.parametrize("entry", [{}, {"labels": None}, {"labels": []}])
def test_missing_or_empty_labels_are_an_empty_set(monkeypatch, entry):
    _registry(monkeypatch, entry)
    assert kp.label_machines(()) == ["worker-1"]
    assert kp._machine_picks_up("worker-1") is True
    assert kp.label_machines("pool:worker") == []


def test_well_formed_labels_still_qualify(monkeypatch):
    _registry(monkeypatch, {"labels": ["pool:worker", "capability:hub-write"]})
    assert kp.label_machines(("pool:worker", "capability:hub-write")) == ["worker-1"]
    assert kp._machine_picks_up("pool:worker") is True
    assert kp._machine_picks_up("worker-1") is True
