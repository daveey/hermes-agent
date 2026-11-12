"""Re-park escalation must not freeze a live natural-condition wait (t_f6d930b7, 11-11/11-12). [daveey]

The Upkeep P5 Mac card parked on a read-only probe (``mac-final-ready.py``: exit 0 only while the Mac is unlocked
and HID idle > 900 s). The probe fired, but by the time a worker ran the user was back, so it re-parked on the same
probe. The third such park escalated: the card went to ``todo`` behind an "Unstick ... parked 3x" owner, its
wake_check was cleared, and the owner ended up on ``mac-ops``, which no pool worker can run.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_awaiting as kaw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as kwc

MAC_PROBE = "/usr/bin/python3 -S /home/ubuntu/code/upkeep-system/mac/mac-final-ready.py"
OTHER_PROBE = "/usr/bin/python3 -S /home/ubuntu/code/upkeep-system/mac/other-ready.py"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    monkeypatch.setenv("ASK_GATE_LEDGER", str(home / "state" / "ask-ledger.sqlite"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    yield home
    kwc._shutdown_for_tests()


def _tool(name, args):
    from tools import kanban_tools as kt
    return json.loads(getattr(kt, f"_handle_{name}")(args))


def _claim(monkeypatch, conn, tid) -> None:
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb.get_task(conn, tid).current_run_id))


def _mac_park(n: int, probe: str = MAC_PROBE) -> dict:
    """The t_f6d930b7 park shape: a declared clock window, a live read-only probe, check_every 300."""
    idle = (53.029, 126.331, 0.038, 46.34, 210.5)[n % 5]
    return {
        "reason": (f"SCHEDULED_UNTIL=2026-11-19T05:{n:02d}:00Z \u2014 Noninterference wait: installed build reachable "
                   f"and unlocked but fresh HID idle {idle}s, so no navigation or actions were sent."),
        "wake_check": probe, "check_every": 300,
        "awaiting": {"kind": "clock", "ref": "Mac HID noninterference quiet window",
                     "why": f"Read-only ioreg observed unlocked with {idle} seconds idle; the probe needs >900 s."}}


def _probe_fires(conn, tid, cmd: str = MAC_PROBE) -> None:
    """The dispatcher's probe: a few `not yet` readings, then the window opens (confirmed exit 0)."""
    state = kwc.get_state(conn, tid)
    assert state and state["cmd"] == cmd and state["every"] == 300, state
    for _ in range(2):
        assert kwc.apply_result(conn, tid, cmd, 1, "unlocked, HID idle 40s", generation=state["generation"]) is None
    assert kwc.apply_result(conn, tid, cmd, 0, "Mac unlocked and HID idle 1055s", generation=state["generation"],
                            diagnostics={"first_rc": 0, "confirm_rc": 0}) == kwc.CONDITION_MET_EVENT
    assert kb.get_task(conn, tid).status == "ready"


def _deadline_wakes(conn, tid) -> None:
    assert kb.unblock_task(conn, tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))


def _mac_card(conn) -> str:
    tid = kb.create_task(conn, title="Upkeep system P5 Mac", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    return tid


def _owners(conn) -> list:
    return conn.execute("SELECT id FROM tasks WHERE title LIKE 'Unstick %'").fetchall()


def test_reparks_after_the_same_probe_fired_never_escalate(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        tid = _mac_card(conn)
        for n in range(5):
            _claim(monkeypatch, conn, tid)
            out = _tool("schedule", _mac_park(n))
            assert out.get("status") == "scheduled", (n, out)
            # The idle trigger stays armed on every park, at its own cadence.
            state = kwc.get_state(conn, tid)
            assert state["cmd"] == MAC_PROBE and state["every"] == 300
            _probe_fires(conn, tid)
        assert not _owners(conn)
        assert not conn.execute("SELECT 1 FROM task_links WHERE child_id=?", (tid,)).fetchone()
        assert not [e for e in kb.list_events(conn, tid) if e.kind == kaw.ESCALATED_EVENT]


def test_same_probe_woken_only_by_its_deadline_still_escalates(kanban_home, monkeypatch) -> None:
    """Control: no fire between parks is no new evidence, however the reason is worded."""
    with kbc.connect_closing() as conn:
        tid = _mac_card(conn)
        outs = []
        for n in range(3):
            _claim(monkeypatch, conn, tid)
            outs.append(_tool("schedule", _mac_park(n)))
            if outs[-1].get("status") == "scheduled":
                _deadline_wakes(conn, tid)
        assert [o.get("status") for o in outs] == ["scheduled", "scheduled", "todo"], outs
        assert outs[2].get("code") == kaw.ESCALATED_EVENT


def test_a_fire_of_another_probe_is_not_progress_for_this_one(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        tid = _mac_card(conn)
        _claim(monkeypatch, conn, tid)
        assert _tool("schedule", _mac_park(0, OTHER_PROBE)).get("status") == "scheduled"
        _probe_fires(conn, tid, OTHER_PROBE)
        outs = []
        for n in range(1, 4):
            _claim(monkeypatch, conn, tid)
            outs.append(_tool("schedule", _mac_park(n)))
            if outs[-1].get("status") == "scheduled":
                _deadline_wakes(conn, tid)
        assert [o.get("status") for o in outs] == ["scheduled", "scheduled", "todo"], outs


def test_one_fire_resets_the_count_but_later_stuck_parks_count_again(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        tid = _mac_card(conn)
        statuses = []
        for n, wake in enumerate(("deadline", "fire", "deadline", "deadline", None)):
            _claim(monkeypatch, conn, tid)
            out = _tool("schedule", _mac_park(n))
            statuses.append(out.get("status"))
            if wake == "fire":
                _probe_fires(conn, tid)
            elif wake == "deadline" and out.get("status") == "scheduled":
                _deadline_wakes(conn, tid)
        # Parks 0-1 then the fire; parks 2-3 after it are two in a row; park 4 is the third: escalates.
        assert statuses == ["scheduled", "scheduled", "scheduled", "scheduled", "todo"], statuses


# -- the escalation owner never lands on a profile nothing can run ---------------------------------------------

@pytest.fixture
def pool(monkeypatch):
    """A hub gateway whose portable cards go to worker-1; mac-ops exists as a profile and is allowlisted."""
    from hermes_cli import config
    from hermes_cli import config_backend
    from hermes_cli import kanban_assignment_routing as ar
    from hermes_cli import kanban_placement as kp
    from hermes_cli import kanban_worker_node as wn
    from hermes_cli import profiles

    cfg = {"kanban": {"orchestrator_profile": "mac-ops",
                      "placement": {"worker_pools": {"hub": ["worker-1"]}, "assignees": ["default", "mac-ops"]}}}
    entry = {"state": "active", "pickup": "explicit", "last_seen": time.time() * 1000, "labels": ["profile:default"]}
    monkeypatch.setattr(config, "load_config_readonly", lambda: cfg)
    monkeypatch.setattr(config_backend, "config_exists", lambda path: True)
    monkeypatch.setattr(profiles, "resolve_profile_env", lambda name: f"/profiles/{name}")
    monkeypatch.setattr(ar, "validate_assignment", lambda name, **kw: [])
    monkeypatch.setattr(kp.reg, "self_gateway", lambda: "hub")
    monkeypatch.setattr(wn, "registry", lambda: {"machines": {"worker-1": entry}})
    return cfg, entry


def _task(**kw):
    base = {"assignee": "default", "created_by": "default", "requires": [], "workspace_kind": "scratch",
            "workspace_path": None, "skills": None}
    return SimpleNamespace(**{**base, **kw})


def test_supervisor_skips_a_profile_no_pool_worker_carries(pool) -> None:
    from hermes_cli import kanban_assignment_routing as ar
    assert ar._supervisor(_task()) == (None, [])
    assert kaw._supervisor_profile(_task()) is None  # the owner goes to triage, not to mac-ops


def test_supervisor_keeps_a_profile_a_live_pool_worker_carries(pool) -> None:
    from hermes_cli import kanban_assignment_routing as ar
    _, entry = pool
    entry["labels"] = ["profile:default", "profile:mac-ops"]
    assert ar._supervisor(_task())[0] == "mac-ops"
    entry["hold"] = True  # a held worker leases nothing
    assert ar._supervisor(_task()) == (None, [])


def test_supervisor_falls_through_to_the_next_runnable_candidate(pool) -> None:
    from hermes_cli import kanban_assignment_routing as ar
    assert ar._supervisor(_task(assignee="worker", created_by="default"))[0] == "default"


@pytest.mark.parametrize("change", ["no_pool", "not_allowlisted"])
def test_local_profiles_stay_runnable_when_the_card_would_not_enter_the_pool(pool, change, monkeypatch) -> None:
    from hermes_cli import kanban_assignment_routing as ar
    from hermes_cli import kanban_worker_node as wn
    cfg, _ = pool
    placement = cfg["kanban"]["placement"]
    if change == "no_pool":
        placement.pop("worker_pools")
    else:
        placement["assignees"] = ["default"]
    monkeypatch.setattr(wn, "registry", lambda: pytest.fail("registry read for a card that never enters the pool"))
    assert ar._supervisor(_task())[0] == "mac-ops"


def test_unreadable_registry_never_invents_a_runnable_pool(pool, monkeypatch) -> None:
    from hermes_cli import kanban_profile_runnable as kpr
    from hermes_cli import kanban_worker_node as wn

    def boom():
        raise RuntimeError("no board identity")
    monkeypatch.setattr(wn, "registry", boom)
    assert kpr.runnable("mac-ops") is False
    monkeypatch.setattr(wn, "registry", lambda: None)
    assert kpr.runnable("mac-ops") is False
