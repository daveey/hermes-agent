"""A move onto any ``machine:<x>`` needs a host_reason naming state on x; capacity is never one. [daveey]

t_01a1d95c41620e6c (hub board, 2026-11-13): three pickup-stall alarm moves pinned published, portable work from one
full machine to another (worker-7 -> worker-1/-4, worker-3 -> worker-8) with no host_reason; two stalled again on the
new machine. Before this change only a pool worker's pin needed a host reason, so ``machine:<non-pool>`` passed bare.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_move_pool as kmp


class Refused(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


POOL = kmp.pool_label()
MACHINES = {"worker-1": {"labels": ["linux", POOL]}, "worker-8": {"labels": ["linux"]}, "hub": {"labels": ["linux"]},
            "macbook": {"labels": ["macos"]}}


def _check(labels, host_reason=None, **kw):
    return kmp.check(labels, MACHINES, me="hub", running=False, previous=["machine:worker-7"], workspace_path=None,
                     host_reason=host_reason, force=kw.get("force", False), refused=Refused)


@pytest.mark.parametrize("target", ["worker-1", "worker-8", "hub", "macbook", "not-enrolled"])
def test_any_machine_target_without_a_host_reason_is_refused_pointing_at_the_pool(target):
    with pytest.raises(Refused) as err:
        _check([f"machine:{target}"])
    assert err.value.code == kmp.HOST_REASON_REQUIRED
    assert POOL in err.value.message and "--pool" in err.value.message and f"machine:{target}" in err.value.message


def test_force_does_not_waive_the_host_reason():
    with pytest.raises(Refused):
        _check(["machine:worker-8"], force=True)


@pytest.mark.parametrize("reason", [
    "worker-7 10/10, no pool worker eligible now",  # the 11-13 alarm move, verbatim shape
    "worker-8 has free slots", "free slot on worker-8", "capacity", "worker-7 is full", "worker-8 is idle",
])
def test_a_capacity_only_host_reason_is_refused(reason):
    with pytest.raises(Refused) as err:
        _check(["machine:worker-8"], host_reason=reason)
    assert err.value.code == kmp.HOST_REASON_CAPACITY and POOL in err.value.message


@pytest.mark.parametrize("reason", [
    "the card's worktree ~/code/_wt/foo is on worker-8",
    "unpushed commits on worker-8",
    "suite PID 4242 and its logs are on worker-8",
    "Mac-only work: needs Xcode",
    "hub-only work: reads the hub DB",
    "worker-7 is 10/10 but the card's worktree with unpublished work is on worker-8",  # state wins over capacity
])
def test_a_host_reason_naming_target_state_is_accepted_and_recorded(reason):
    assert _check(["machine:worker-8"], host_reason=reason) == {"host_reason": reason}


@pytest.mark.parametrize("labels", [[POOL], ["linux"], ["macos", "capability:xcode"]])
def test_labels_that_name_no_machine_need_no_host_reason(labels):
    assert _check(labels) == {}


def test_machine_pins_reads_every_machine_label():
    assert kmp.machine_pins(["linux", "machine:a", "machine:b"]) == ["a", "b"]
    assert kmp.pool_pins(["machine:worker-1", "machine:worker-8"], MACHINES) == ["worker-1"]
