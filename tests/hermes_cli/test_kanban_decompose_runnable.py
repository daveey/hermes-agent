"""The decomposer never hands an enrolled worker-first card to a profile no pool worker runs. [daveey]

Two escalation owners were created in triage, enrolled ``worker-first-v1`` (assignee None -> default, which is
allowlisted), and the auto-decomposer then specified them to ``mac-ops``. Placement is fixed at creation, so each
then needed ``profile:mac-ops``, which no pool worker carries, and sat queued until an orchestrator finished it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp

PROFILES = ["default", "mac-ops", "researcher"]


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A hub gateway whose pool is worker-1 (profile:default only); every profile is installed."""
    from hermes_cli import config, profiles
    from hermes_cli import kanban_placement as kp
    from hermes_cli import kanban_worker_node as wn

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    cfg = {"kanban": {"default_assignee": "default",
                      "placement": {"worker_pools": {"hub": ["worker-1"]}, "assignees": ["default", "mac-ops"]}}}
    entry = {"state": "active", "pickup": "explicit", "last_seen": time.time() * 1000, "labels": ["profile:default"]}
    monkeypatch.setattr(config, "load_config_readonly", lambda: cfg)
    monkeypatch.setattr(kp.reg, "self_gateway", lambda: "hub")
    monkeypatch.setattr(wn, "registry", lambda: {"machines": {"worker-1": entry}})
    fakes = [SimpleNamespace(name=n, description=f"desc for {n}") for n in PROFILES]
    monkeypatch.setattr(profiles, "list_profiles", lambda: fakes)
    monkeypatch.setattr(profiles, "profile_exists", lambda n: n in PROFILES)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    return cfg, entry


def _triage(*, enrolled: bool = True) -> str:
    """A triage card with no assignee, enrolled by create_task itself (portable: default is allowlisted)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="escalation owner", triage=True)
        assert kb.get_task(conn, tid).placement_policy == "worker-first-v1"
        if not enrolled:
            conn.execute("UPDATE tasks SET placement_policy=NULL WHERE id=?", (tid,))
            conn.commit()
    return tid


def _decompose(tid: str, payload: dict):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = json.dumps(payload)
    with patch("agent.auxiliary_client.call_llm", return_value=resp) as llm:
        outcome = decomp.decompose_task(tid, author="me")
    assert outcome.ok, outcome.reason
    return outcome, llm


def _assignee(tid: str):
    with kbc.connect() as conn:
        return kb.get_task(conn, tid).assignee


SINGLE = {"fanout": False, "title": "Fix the Mac thing", "body": "do it", "assignee": "mac-ops"}


def test_enrolled_card_never_specified_to_a_profile_no_pool_worker_runs(board) -> None:
    tid = _triage()
    _, llm = _decompose(tid, SINGLE)
    assert _assignee(tid) == "default"
    prompt = llm.call_args.kwargs["messages"][-1]["content"]
    assert "mac-ops ⚠ no pool worker runs it" in prompt and "default ⚠" not in prompt


def test_enrolled_card_keeps_a_pick_a_live_pool_worker_carries(board) -> None:
    board[1]["labels"].append("profile:mac-ops")
    tid = _triage()
    _decompose(tid, SINGLE)
    assert _assignee(tid) == "mac-ops"


def test_enrolled_card_ignores_an_unrunnable_default_assignee(board) -> None:
    board[0]["kanban"]["default_assignee"] = "mac-ops"
    tid = _triage()
    _decompose(tid, SINGLE)
    assert _assignee(tid) is None  # unchanged: the assignee it was enrolled under


def test_unenrolled_card_accepts_any_installed_profile(board, monkeypatch) -> None:
    from hermes_cli import kanban_worker_node as wn
    monkeypatch.setattr(wn, "registry", lambda: pytest.fail("registry read for a card outside the pool"))
    tid = _triage(enrolled=False)
    _decompose(tid, SINGLE)
    assert _assignee(tid) == "mac-ops"


def test_fanout_root_of_an_enrolled_card_never_goes_to_an_unrunnable_orchestrator(board) -> None:
    board[0]["kanban"]["orchestrator_profile"] = "mac-ops"
    tid = _triage()
    outcome, _ = _decompose(tid, {"fanout": True, "tasks": [
        {"title": "on the Mac", "body": "b", "assignee": "mac-ops", "parents": [], "requires": []},
    ]})
    assert _assignee(tid) == "default"
    # Children are inserted unenrolled and run on this gateway, which has every profile.
    assert _assignee(outcome.child_ids[0]) == "mac-ops"
