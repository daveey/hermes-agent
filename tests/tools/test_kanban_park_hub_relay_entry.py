"""The public park entry points (tools and CLI) relay a hub card's board probe from a worker node. [daveey]

The park helpers skip the dry run for a probe the hub will run (``kanban_wake_check_hub_relay``) only when the entry
point hands them the board and the card: these drive the real tool and slash-command entry points on a worker's copy
of a hub card, with a probe whose dry run on the worker would refuse the park as "already true".
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli import test_kanban_wake_check_home as home_tests
from tests.hermes_cli import test_kanban_wake_check_hub_relay as relay_tests
from tests.hermes_cli.test_kanban_worker_node import HUB
from tools import kanban_tools  # registers the real entry points
from tools.registry import registry

gws = home_tests.gws
PROBE = relay_tests.ALREADY_TRUE_ON_THE_WORKER
UNTIL = relay_tests.UNTIL


@pytest.fixture
def worker_board(gws, monkeypatch):
    hub, worker = gws
    monkeypatch.delenv("HERMES_KANBAN_WAKE_DRYRUN", raising=False)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_TASK", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_RUN_ID",
                "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    copy, _ = relay_tests._running_copy(hub, worker)
    worker.enter()
    own = kb.create_task(worker.conn, title="the worker's own card", assignee="default")
    assert kb.claim_task(worker.conn, own)
    return worker, copy, own


def _as_worker(monkeypatch, worker, tid):
    """Park as the card's own worker (the live-claim guard refuses anyone else)."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb.get_task(worker.conn, tid).current_run_id))


def _tool(tool, tid):
    args = {"task_id": tid, "wake_check": PROBE, "check_every": 300, "awaiting": home_tests.BUILD}
    if tool == "kanban_continue":
        args.update(checkpoint="waiting for the hub's FIX card", resume_after_seconds=86400)
    else:
        args["reason"] = UNTIL
        if tool == "kanban_block":
            args["kind"] = "time"
    return json.loads(registry.dispatch(tool, args))


@pytest.mark.parametrize("tool", ["kanban_block", "kanban_continue", "kanban_schedule"])
def test_the_tools_relay_a_hub_cards_board_probe_without_a_dry_run(worker_board, monkeypatch, tool):
    worker, copy, own = worker_board
    _as_worker(monkeypatch, worker, copy)
    res = _tool(tool, copy)
    assert res.get("ok"), res
    assert relay_tests._last_park(worker.conn, copy)["probe_host"] == HUB
    assert kwc.get_state(worker.conn, copy) is None
    _as_worker(monkeypatch, worker, own)
    refused = _tool(tool, own)   # the worker's own card: dry run as before
    assert kwc.WAKE_CHECK_ALREADY_TRUE in json.dumps(refused), refused


@pytest.mark.parametrize("verb", ["schedule", "block"])
def test_the_cli_relays_a_hub_cards_board_probe_without_a_dry_run(worker_board, monkeypatch, verb):
    worker, copy, own = worker_board
    tail = ("SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting for the FIX card --wake-check "
            f"'{PROBE}' --check-every 300 "
            "--awaiting 'external:release build 559' --awaiting-why 'the build job writes the flag'")
    kind = " --kind time" if verb == "block" else ""
    _as_worker(monkeypatch, worker, copy)
    out = kc.run_slash(f"{verb} {copy} {tail}{kind}")
    assert kb.get_task(worker.conn, copy).status == "scheduled", out
    assert relay_tests._last_park(worker.conn, copy)["probe_host"] == HUB
    assert kwc.get_state(worker.conn, copy) is None
    _as_worker(monkeypatch, worker, own)
    out = kc.run_slash(f"{verb} {own} {tail}{kind}")
    assert kwc.WAKE_CHECK_ALREADY_TRUE in out, out
    assert kb.get_task(worker.conn, own).status == "running"
