"""Malformed Board registry ``labels`` fail closed in Kanban placement liveness. [daveey]

PR #2171 review nit (t_01a1d4be7c79b6e4, GitHub review 5890499417; fixed in t_01a1d4c327277a9d):
``label_machines`` evaluated ``wanted <= set(entry.get("labels") or [])``. A non-iterable ``labels`` (an int) raised
TypeError and a str matched its characters (``"x"`` carried label ``x``). Since #2171 the named-machine branch of
``_machine_picks_up`` also goes through ``label_machines`` (empty label set), so one malformed entry crashed named
routing too. Like ``kanban_placement_coordination._carries``, labels must be a list of str; a missing or null list is
empty; any other shape does not qualify.

PR #2202 review nits (t_01a1d4f2803ab733, GitHub review 5891340823; fixed in t_01a1d4fa8dc195ef):
``markers_unmeetable_here`` tested ``"linux" in (entry.get("labels") or [])``, the same shape bug: an int raised
TypeError and a str (``"linux"``, ``"linux-arm"``) substring-matched. It now shares ``label_machines``'s list-of-str
check (module-level ``registry_carries``), failing closed.
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


@pytest.mark.parametrize("labels", [5, "linux", "linux-arm", {"linux": 1}, ("linux",), ["linux", 5], None])
def test_malformed_labels_are_not_linux(labels):
    machines = {"worker-1": {"state": "active", "labels": labels}}
    assert kp.markers_unmeetable_here("worker-1", {}, machines) is False


def test_linux_list_label_is_linux():
    assert kp.markers_unmeetable_here("worker-1", {}, {"worker-1": {"labels": ["pool:worker", "linux"]}}) is True
    assert kp.markers_unmeetable_here("worker-1", {}, {"worker-1": {"labels": ["linux-arm"]}}) is False


@pytest.mark.parametrize("entry, ok", [({}, True), ({"labels": None}, True), ({"labels": []}, True),
                                       ({"labels": ["a"]}, True), ({"labels": 5}, False), ({"labels": "a"}, False),
                                       ({"labels": ["a", 5]}, False)])
def test_registry_carries_shape(entry, ok):
    assert kp.registry_carries(entry, set()) is ok
    assert kp.registry_carries(entry, {"a"}) is (ok and entry.get("labels") == ["a"])
