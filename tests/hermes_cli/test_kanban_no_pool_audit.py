"""A profile's scheduling opt-out must not hide shared-board creation diagnostics."""
import json
import re

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_placement as placement
from hermes_cli import kanban_worker_placement as worker


@pytest.mark.parametrize("frontdoor", ["db", "cli", "tool"])
@pytest.mark.parametrize("assignee,body,reason", [
    ("specialist", "", "assignee not allowlisted"),
    ("default", "Inspect special-device", "home gateway marker"),
    ("default", "", None),
])
def test_no_pool_profile_audits_shared_default_board(tmp_path, monkeypatch, frontdoor, assignee, body, reason):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "specialist"
    profile.mkdir(parents=True)
    (root / "config.yaml").write_text("kanban:\n  placement:\n    worker_pools:\n      hub: [worker-1]\n")
    config = "kanban:\n  placement:\n    extra_home_markers: [special-device]\n"
    (profile / "config.yaml").write_text(config)
    board = root / "kanban" / "boards" / "default.db"
    board.parent.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(board))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(placement.reg, "self_gateway", lambda: "hub")
    cfg = placement.config()
    assert "worker_pools" not in cfg
    assert cfg["extra_home_markers"] == ["special-device"]
    kb.init_db()
    args = {"title": "Analyze public data", "assignee": assignee, "body": body, "requires": []}
    if frontdoor == "db":
        with kbc.connect() as conn:
            tid = kb.create_task(conn, **args)
    elif frontdoor == "cli":
        from hermes_cli import kanban as kc
        out = kc.run_slash(f"create 'Analyze public data' --assignee {assignee} --body '{body}'")
        tid = re.search(r"(t_[0-9a-f]+)", out).group(1)
    else:
        from tools import kanban_tools as kt
        result = json.loads(kt._handle_create(args))
        assert result.get("ok"), result
        tid = result["task_id"]
    with kbc.connect() as conn:
        assert conn.execute("PRAGMA database_list").fetchone()[2] == str(board)
        task = kb.get_task(conn, tid)
        event = next(e for e in kb.list_events(conn, tid) if e.kind == "created")
        assert event.payload["portable_reason"] == reason
        assert event.payload["placement_policy"] is None
        assert task.placement_policy is None
    assert worker.creation_decision(args, cfg, "hub", "auto", None) is None
    assert (profile / "config.yaml").read_text() == config


def test_audit_failure_is_visible_not_portable(monkeypatch):
    def broken(*args):
        raise ValueError("invalid classifier input")
    monkeypatch.setattr(placement, "portable_reason", broken)
    assert worker.creation_audit({}, {}) == {"portable_reason": "audit failed"}
