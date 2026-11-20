"""Only recent Board metrics can refuse portable placement."""
import pytest

from hermes_cli import kanban_move as move
from hermes_cli import kanban_placement as placement
from hermes_cli.kanban_placement_coordination import SKEW_S

NOW = 1_800_000_000.0


@pytest.mark.parametrize("at,refuses", [
    (NOW * 1000, True),
    ((NOW - 90) * 1000, True),
    ((NOW - 90.001) * 1000, False),
    (None, False),
    ("1800000000000", False),
    (True, False),
    (float("nan"), False),
    (float("inf"), False),
    (float("-inf"), False),
    ((NOW + SKEW_S) * 1000, True),
    ((NOW + SKEW_S + .001) * 1000, False),
], ids=["fresh", "old-boundary", "stale", "missing", "string", "bool", "nan", "inf", "negative-inf",
        "skew-boundary", "future"])
@pytest.mark.parametrize("route", ["move", "labels", "bare-pickup", "pin"])
def test_claiming_metrics_freshness(monkeypatch, at, refuses, route):
    monkeypatch.setattr("time.time", lambda: NOW)
    entry = {"state": "active", "labels": ["pool:worker", "machine:worker-1"],
             "pickup": "all", "last_seen": NOW * 1000, "metrics": {"claiming": 0}}
    if at is not None:
        entry["metrics"]["at"] = at
    machines = {"worker-1": entry}
    if route == "move":
        matches = move.matching_machines(["pool:worker"], machines, me="hub")
    else:
        wanted = () if route == "bare-pickup" else ["machine:worker-1"] if route == "pin" else "pool:worker"
        matches = placement.label_machines(wanted, machines)
    assert matches == ([] if refuses and route != "pin" else ["worker-1"])


def test_older_core_missing_claiming_is_not_a_refusal(monkeypatch):
    monkeypatch.setattr("time.time", lambda: NOW)
    entry = {"labels": ["pool:worker"], "last_seen": NOW * 1000, "metrics": {"at": NOW * 1000}}
    assert move.matching_machines(["pool:worker"], {"worker-1": entry}, me="hub") == ["worker-1"]
    assert placement.label_machines("pool:worker", {"worker-1": entry}) == ["worker-1"]


@pytest.mark.parametrize("metrics", [None, [], {"claiming": 1, "at": NOW * 1000},
                                    {"claiming": 0, "at": 10 ** 400}])
def test_unknown_or_out_of_window_metrics_do_not_refuse(metrics):
    entry = {"labels": ["pool:worker"], "metrics": metrics}
    assert not move._not_claiming(entry, now=NOW)
    assert move.matching_machines(["pool:worker"], {"worker-1": entry}, me="hub", now=NOW) == ["worker-1"]
    assert placement._claiming_for("worker-1", entry, {"pool:worker"}, now=NOW)


def test_explicit_now_is_honoured_by_every_caller():
    """``now`` must reach ``_not_claiming``; the real clock (unpatched) is years after NOW, so a sample fresh at
    NOW is stale on it and only a threaded ``now`` refuses."""
    entry = {"labels": ["pool:worker"], "metrics": {"claiming": 0, "at": NOW * 1000}}
    assert move._not_claiming(entry, now=NOW)
    assert move.matching_machines(["pool:worker"], {"worker-1": entry}, me="hub", now=NOW) == []
    assert not placement._claiming_for("worker-1", entry, {"pool:worker"}, now=NOW)
