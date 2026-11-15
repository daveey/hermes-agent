"""Ready notices use the same live update-hold evidence as admission."""
from __future__ import annotations

import json

import pytest

from agent import core_update_drain as drain
from hermes_cli import kanban_chat_wait as kcw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli import test_kanban_label_backlog_wait as backlog
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
    text = helpers._notice(live, helpers._card(live))
    assert "capacity first" in text and "bump" not in text


# --- this host's hold only explains cards this host leases (t_01a1e01b9645d70c) --------------------------------

hub = backlog.hub
HOLD_WORDS = "pending Hermes " + "update"


def _hold(monkeypatch, tmp_path, active=True):
    marker = tmp_path / "drain-hold"
    marker.write_text(json.dumps({"pid": 12345, "started_at": START, "reason": "pending core update"}))
    monkeypatch.setattr(drain, "hold_paths", lambda: [marker] if active else [])
    monkeypatch.setattr(drain, "_pid_alive", lambda pid: pid == 12345)


def _swept(conn, tid):
    """The card's notice from a real dispatcher sweep, then cleared so the next sweep posts the episode again."""
    assert kcw.sweep(conn) >= 1
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, kcw.NOTICE_KIND)).fetchone()
    assert row is not None, "the due card got no notice"
    with kb.write_txn(conn):
        conn.execute("DELETE FROM task_events WHERE kind=?", (kcw.NOTICE_KIND,))
    payload = json.loads(row[0])
    return payload, kcw.notice_text(payload)


def _stable(payload):
    return {k: v for k, v in payload.items() if k not in ("observed_at", "checkpoint_diagnostic")}


@pytest.mark.parametrize("claims", [0, 3])
def test_hub_hold_does_not_explain_a_pool_card_the_hub_cannot_lease(hub, tmp_path, monkeypatch, claims):
    tid = backlog._seed(hub.conn, claims=claims)
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET session_id='origin' WHERE id=?", (tid,))  # a chat waits on it
    _hold(monkeypatch, tmp_path, active=False)
    plain, plain_text = _swept(hub.conn, tid)
    _hold(monkeypatch, tmp_path, active=True)
    held, text = _swept(hub.conn, tid)
    assert not any(k.startswith("core_update_") for k in held), held
    assert held["admission"]["reason"] != "core_update_hold", held
    assert HOLD_WORDS not in text and "2 card(s) with matching labels ahead of it" in text, text
    assert _stable(held) == _stable(plain)  # exactly the notice it gets with no hold active
    assert text.split(" [board")[0] == plain_text.split(" [board")[0]


def test_hub_hold_does_not_explain_a_worker_pool_card(live, tmp_path, monkeypatch):
    from hermes_cli.kanban_worker_placement import POLICY
    _hold(monkeypatch, tmp_path)
    tid = helpers._card(live)
    with kb.write_txn(live):
        live.execute("UPDATE tasks SET placement_policy=? WHERE id=?", (POLICY, tid))
    payload, text = _swept(live, tid)
    assert "core_update_hold" not in payload and payload["admission"]["reason"] != "core_update_hold", payload
    assert "waiting for a pool worker" in text and HOLD_WORDS not in text, text


def test_hub_hold_still_explains_a_card_this_host_leases(live, tmp_path, monkeypatch):
    _hold(monkeypatch, tmp_path)
    payload, text = _swept(live, helpers._card(live))
    assert payload["core_update_hold"] is True
    assert payload["admission"] == {"reason": "core_update_hold", "status": "held"}, payload
    assert "claims are held for a " + HOLD_WORDS in text, text


def test_no_hold_leaves_a_local_card_notice_unchanged(live, tmp_path, monkeypatch):
    _hold(monkeypatch, tmp_path, active=False)
    payload, text = _swept(live, helpers._card(live))
    assert "core_update_hold" not in payload and payload["admission"]["reason"] != "core_update_hold", payload
    assert HOLD_WORDS not in text and "capacity first" in text, text
