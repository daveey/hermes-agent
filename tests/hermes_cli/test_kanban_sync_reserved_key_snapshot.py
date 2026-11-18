"""Refuse torn/mislabelled remote uniqueness scans before reserved-key recovery writes."""
import pytest

from hermes_cli import kanban_sync_reserved_key as rk
from tests.hermes_cli import test_kanban_sync_reserved_key_safety as helpers

kanban_home = helpers.kanban_home
svc = helpers.svc
live = helpers.live


@pytest.mark.parametrize("fault", ["head_changed", "head_zero_changed", "foreign_table", "missing_table", "later_table"])
def test_snapshot_identity_refuses_preview_and_apply(live, svc, monkeypatch, fault):
    tid, _ = helpers.bad_card(live, svc, monkeypatch)
    hub = svc.hub(helpers.ME)
    token = helpers.plan_with(live, hub, tid)["token"]
    before = helpers.dump(live)

    def snapshot(board, table, after=""):
        page = {"head": 0 if fault == "head_zero_changed" else 1, "table": "tasks",
                "rows": [], "more": not after, "next": "a"}
        if fault in ("head_changed", "head_zero_changed") and after:
            page["head"] = 2
        if fault == "foreign_table" or (fault == "later_table" and after):
            page["table"] = "task_comments"
        if fault == "missing_table":
            del page["table"]
        return page

    monkeypatch.setattr(hub, "snapshot_page", snapshot)
    # A moving scan is insufficient alone: no complete interval proof is available.
    monkeypatch.setattr(hub, "changes", lambda *a: {})
    report = helpers.plan_with(live, hub, tid)
    assert not report["eligible"], report
    assert report["refusal"].startswith("authoritative_snapshot_incomplete"), report
    result = rk.apply(live, hub, helpers.BOARD, tid, helpers.GOOD, token, reason=helpers.REASON)
    assert result["action"] == "refused", result
    assert helpers.dump(live) == before


@pytest.mark.parametrize("head", [0, 7])
def test_stable_tasks_snapshot_allows_recovery(live, svc, monkeypatch, head):
    tid, _ = helpers.bad_card(live, svc, monkeypatch)
    hub = svc.hub(helpers.ME)
    calls = []

    def snapshot(board, table, after=""):
        calls.append(after)
        return {"head": head, "table": "tasks", "rows": [], "more": not after, "next": "a"}

    monkeypatch.setattr(hub, "snapshot_page", snapshot)
    # A moving scan is insufficient alone: no complete interval proof is available.
    monkeypatch.setattr(hub, "changes", lambda *a: {})
    report = helpers.plan_with(live, hub, tid)
    assert report["eligible"], report
    assert calls == ["", "a"]
    result = rk.apply(live, hub, helpers.BOARD, tid, helpers.GOOD, report["token"], reason=helpers.REASON)
    assert result["action"] == "applied", result
