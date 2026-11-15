"""Abuse and concurrent admission controls for the immutable decision store."""
import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from tests.hermes_cli import test_origin_forward_decision as helpers

forwarding = helpers.forwarding
authority = helpers.authority
decide = helpers.decide


@pytest.mark.parametrize("key,value", [("origin", {}), ("owner", {}), ("title", ""),
    ("session_id", "../chat"), ("body", 3), ("skills", "git"), ("requires", [3]),
    ("priority", True), ("triage", 1)])
def test_create_surface_is_not_arbitrary_tool_dispatch(forwarding, key, value):
    changed = copy.deepcopy(forwarding[0])
    changed["args"]["create"][key] = value
    with pytest.raises(ValueError, match="origin_forward_create_invalid"):
        decide(forwarding, changed)
    assert not forwarding[2]


def test_oversized_payload_refused_before_storage(forwarding):
    changed = copy.deepcopy(forwarding[0])
    changed["args"]["create"]["body"] = "x" * 262145
    with pytest.raises(ValueError, match="origin_forward_request_invalid"):
        decide(forwarding, changed)
    assert not forwarding[1]["path"].exists()


def test_concurrent_conflicting_admissions_only_freeze_one(forwarding):
    # Initialize an empty store before the racing network reads.
    with pytest.raises(ValueError):
        decide(forwarding, lease_lookup=lambda *args: None)
    gate = Barrier(2)
    changed = copy.deepcopy(forwarding[0])
    changed["args"]["create"]["title"] = "different"

    def lookup(*args):
        gate.wait(timeout=10)
        return {"task_id": "task-1", "holder": "worker-7", "run_uid": "run-1", "expires": 2000}

    def run(req):
        try:
            return decide(forwarding, req, lease_lookup=lookup)
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, [forwarding[0], changed]))
    assert sum(isinstance(item, dict) for item in outcomes) == 1
    assert "origin_forward_conflict" in outcomes
