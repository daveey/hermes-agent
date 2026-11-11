"""Ready notices use the same live update-hold evidence as admission."""
from __future__ import annotations

import json

import pytest

from agent import core_update_drain as drain
from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli import test_kanban_missing_board_notice as helpers

kanban_home = helpers.kanban_home
svc = helpers.svc
live = helpers.live
START = 1794403906  # 2026-11-11 05:31:46 PST


@pytest.mark.parametrize("missing", [False, True])
def test_active_drain_explains_hold_instead_of_capacity(live, tmp_path, monkeypatch, missing):
    marker = tmp_path / "drain-hold"
    marker.write_text(json.dumps({"pid": 12345, "started_at": START, "reason": "pending core update"}))
    monkeypatch.setattr(drain, "hold_paths", lambda: [marker])
    monkeypatch.setattr(drain, "_pid_alive", lambda pid: pid == 12345)
    tid = helpers._card(live)
    if missing:
        monkeypatch.setattr(sync._client(), "acquire", lambda **kw: sync.Lease(
            granted=False, run_uid=kw["run_uid"], reason="not_found"))
        sync.claim_gate(live, tid, START + 900)
    text = helpers._notice(live, tid)
    assert "claims are held for a pending Hermes update" in text
    assert "2026-11-11 05:31:46 PST" in text
    assert "until the update applies or the drain's hard max" in text
    assert "priority" not in text and "re-place" not in text


@pytest.mark.parametrize("started", [None, "bad", float("inf")])
def test_drain_without_valid_start_still_reports_hold(live, tmp_path, monkeypatch, started):
    marker = tmp_path / "drain-hold"
    marker.write_text(json.dumps({"pid": 12345, "started_at": started}))
    monkeypatch.setattr(drain, "hold_paths", lambda: [marker])
    monkeypatch.setattr(drain, "_pid_alive", lambda pid: True)
    text = helpers._notice(live, helpers._card(live))
    assert "hold started at an unknown time" in text
    assert "pending Hermes update" in text and "priority" not in text


def test_dead_drain_writer_does_not_hide_capacity_advice(live, tmp_path, monkeypatch):
    marker = tmp_path / "drain-hold"
    marker.write_text(json.dumps({"pid": 12345, "started_at": START}))
    monkeypatch.setattr(drain, "hold_paths", lambda: [marker])
    monkeypatch.setattr(drain, "_pid_alive", lambda pid: False)
    assert "priority" in helpers._notice(live, helpers._card(live))
