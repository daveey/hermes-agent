"""Host-level concurrency accounting + review-lane fairness (OOF-30 review).

Three gaps found in review of the original memory-guard PR:

1. The standalone daemon path (``hermes kanban daemon --force`` /
   :func:`hermes_cli.kanban_db_dispatch.run_daemon`) never resolved
   ``kanban.max_in_progress`` at all — the one shipped entry point that
   could still fan out an entire backlog in a single tick.
2. ``max_in_progress`` was enforced per-board while the gateway dispatcher
   ticks every active board — N boards multiplied the host budget by N.
3. The ready loop consumed the entire shared spawn budget before the
   review loop ran, so a sustained ready backlog starved autonomous
   reviews indefinitely.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch, all_assignees_spawnable):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    from hermes_cli.kanban_model_assignment import _assignee_config
    (home / "config.yaml").write_text(_managed_yaml())
    for name in ("alice", "reviewer"):
        assert _assignee_config(name) is None
        profile = home / "profiles" / name
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text(_managed_yaml())
        assert _assignee_config(name)["model_routing"]["enabled"] is True
    from tests.hermes_cli.kanban_review_fixture import completed_source
    with kbc.connect() as conn:
        completed_source(conn)
    return home


def _set_task_status(conn: sqlite3.Connection, task_id: str, status: str) -> None:
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


def _fake_spawn_factory(spawns: list):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42
    return fake_spawn


# ---------------------------------------------------------------------------
# 1. Standalone daemon resolves max_in_progress (P1a)
# ---------------------------------------------------------------------------


def test_run_daemon_resolves_and_passes_max_in_progress(
    kanban_home, monkeypatch,
):
    """The daemon tick must pass a resolved cap into dispatch_once.

    Regression guard for the OOF-30 review finding: ``run_daemon`` only
    forwarded ``max_spawn`` — with no explicit ``--max`` (the shipped
    systemd shape) nothing capped the tick even though the gateway and
    ``hermes kanban dispatch`` paths both resolved the memory-derived
    default.
    """
    captured: dict = {}
    stop = threading.Event()

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    # No explicit config → the derived default must flow through.
    monkeypatch.setattr(kbd, "configured_max_in_progress", lambda: None)
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda sample=None: 3)

    def on_tick(res):
        stop.set()

    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=on_tick)

    assert captured.get("max_in_progress") == 3




def test_configured_max_in_progress_parsing(monkeypatch):
    import hermes_cli.config as cfgmod

    cases = [
        ({"kanban": {"max_in_progress": 4}}, 4),
        ({"kanban": {"max_in_progress": "5"}}, 5),
        ({"kanban": {"max_in_progress": 0}}, None),
        ({"kanban": {"max_in_progress": -2}}, None),
        ({"kanban": {"max_in_progress": "lots"}}, None),
        ({"kanban": {}}, None),
        ({}, None),
    ]
    for config, expected in cases:
        monkeypatch.setattr(
            cfgmod, "load_config_readonly", lambda c=config: c
        )
        assert kbd.configured_max_in_progress() == expected, config


# ---------------------------------------------------------------------------
# 2. max_in_progress counts running work on ALL boards (P1b)
# ---------------------------------------------------------------------------


def test_max_in_progress_counts_other_boards(
    kanban_home, all_assignees_spawnable,
):
    """Workers running on another board consume the same host budget."""
    kb.create_board("second")

    # Two workers already running on the second board.
    with kbc.connect(board="second") as conn:
        for title in ("busy-1", "busy-2"):
            tid = kb.create_task(conn, title=title, assignee="alice")
            assert kb.claim_task(conn, tid) is not None

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="wants-to-run", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # Host budget (2) already consumed by the second board → nothing spawns.
    assert not spawns
    assert not res.spawned


def test_max_in_progress_partial_budget_across_boards(
    kanban_home, all_assignees_spawnable,
):
    kb.create_board("second")

    with kbc.connect(board="second") as conn:
        tid = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, tid) is not None

    spawns: list = []
    with kbc.connect() as conn:
        for title in ("a", "b", "c"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # 1 running elsewhere + budget 2 → exactly one new spawn here.
    assert len(spawns) == 1
    assert len(res.spawned) == 1


def test_count_running_tasks_other_boards_fails_open(
    kanban_home, monkeypatch,
):
    """A broken board enumeration must not brick dispatch (returns 0)."""
    monkeypatch.setattr(
        kb, "list_boards",
        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert kbd.count_running_tasks_other_boards() == 0


def test_max_spawn_stays_per_board(kanban_home, all_assignees_spawnable):
    """``max_spawn`` keeps its historical per-board semantics."""
    kb.create_board("second")
    with kbc.connect(board="second") as conn:
        tid = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, tid) is not None

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="a", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_spawn=1,
        )

    # The other board's worker does NOT count against max_spawn.
    assert len(spawns) == 1
    assert len(res.spawned) == 1


# ---------------------------------------------------------------------------
# 3. Review lane cannot be starved by a sustained ready backlog (P2)
# ---------------------------------------------------------------------------


def _park_in_review(conn: sqlite3.Connection, title: str, assignee: str) -> str:
    from tests.hermes_cli.kanban_review_fixture import create_review
    return create_review(conn, title, assignee)


@pytest.mark.parametrize("review_priority,ready_priority,review_spawns", [(900, 100, True), (0, 500, False)])
def test_review_and_ready_share_one_ranked_budget(
    kanban_home, all_assignees_spawnable, monkeypatch, review_priority, ready_priority, review_spawns,
):
    """No reserved review slot: ready and review rows compete by the same admission score."""
    import hermes_cli.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        ready_ids = [kb.create_task(conn, title=t, assignee="alice", priority=ready_priority)
                     for t in ("ready-1", "ready-2", "ready-3")]
        review_id = _park_in_review(conn, "review-me", "reviewer")
        conn.execute("UPDATE tasks SET priority=? WHERE id=?", (review_priority, review_id))
        conn.commit()
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    spawned_ids = [s[0] for s in res.spawned]
    assert len(spawned_ids) == 2
    assert (review_id in spawned_ids) is review_spawns
    assert sum(t in ready_ids for t in spawned_ids) == (1 if review_spawns else 2)


def _guard_review_row(conn: sqlite3.Connection, review_id: str) -> dict:
    """Latest run ``rate_limited`` → ``check_respawn_guard`` returns a cooldown."""
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, "
            "started_at, ended_at) VALUES (?, 'reviewer', 'rate_limited', "
            "'rate_limited', ?, ?)",
            (review_id, now, now),
        )
    assert kbd.check_respawn_guard(conn, review_id, lane="review") == "rate_limit_cooldown"
    return {"max_in_progress": 1}


def _cap_review_row(conn: sqlite3.Connection, review_id: str) -> dict:
    """``reviewer`` already has one running worker → the review row is per-profile capped."""
    busy_id = kb.create_task(conn, title="busy", assignee="reviewer")
    assert kb.claim_task(conn, busy_id) is not None
    return {"max_in_progress": 2, "max_in_progress_per_profile": 1}


@pytest.mark.parametrize("make_unspawnable", [_guard_review_row, _cap_review_row])
def test_unspawnable_review_does_not_reserve_the_only_ready_slot(
    kanban_home, all_assignees_spawnable, monkeypatch, make_unspawnable,
):
    """A review card the review loop would refuse this tick (respawn guard,
    per-profile cap) must not consume the fairness reservation — otherwise the
    ready lane starves every tick while the reserved slot goes unused."""
    import hermes_cli.config as cfgmod

    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        ready_id = kb.create_task(conn, title="ready-now", assignee="alice")
        review_id = _park_in_review(conn, "review-unspawnable", "reviewer")
        caps = make_unspawnable(conn, review_id)
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), **caps)

    assert [task_id for task_id, *_ in res.spawned] == [ready_id]


def test_urgent_review_wins_the_only_shared_slot(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """A dispatchable urgent review wins by score, not an unconditional reservation."""
    import hermes_cli.config as cfgmod

    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="ready-now", assignee="alice")
        review_id = _park_in_review(conn, "review-now", "reviewer")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET priority=900 WHERE id=?", (review_id,))
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=1,
        )

    assert [task_id for task_id, *_ in res.spawned] == [review_id]


def test_review_reservation_released_when_no_review_work(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    import hermes_cli.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        for title in ("ready-1", "ready-2", "ready-3"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # No review work → ready lane keeps the full budget.
    assert len(res.spawned) == 2


def test_nonspawnable_review_does_not_tax_ready_budget(
    kanban_home, monkeypatch,
):
    """Review tasks parked for humans (no real profile) release the slot."""
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )
    # Only 'alice' is a real profile; the review assignee is a human lane.
    monkeypatch.setattr(
        profmod, "profile_exists", lambda name: name == "alice"
    )

    spawns: list = []
    with kbc.connect() as conn:
        for title in ("ready-1", "ready-2"):
            kb.create_task(conn, title=title, assignee="alice")
        _park_in_review(conn, "human-review", "some-human")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # Human-lane review is not spawnable → no reservation, ready gets both.
    assert len(res.spawned) == 2


def test_review_budget_still_bounded_by_shared_cap(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """The reservation caps the ready lane; it grants review no extra slots."""
    import hermes_cli.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="ready-1", assignee="alice")
        for i in range(3):
            _park_in_review(conn, f"review-{i}", "reviewer")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # Budget 2 total across both lanes, reservation notwithstanding.
    assert len(res.spawned) == 2


# ---------------------------------------------------------------------------
# 4. A ready queue held only by a full cap is idle, not "stuck" [daveey]
# ---------------------------------------------------------------------------


def test_full_max_in_progress_is_recorded_and_named(kanban_home, all_assignees_spawnable):
    """Ready cards waiting behind max_in_progress running workers: the tick says why (at_capacity)."""
    with kbc.connect() as conn:
        for title in ("busy-1", "busy-2"):
            assert kb.claim_task(conn, kb.create_task(conn, title=title, assignee="alice")) is not None
        kb.create_task(conn, title="waits", assignee="alice")
        spawns: list = []
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2)
    assert not spawns
    assert res.at_capacity == "max_in_progress=2/2"
    assert "at_capacity(max_in_progress=2/2)" in kbd.describe_suppression([res])
    assert kbd.all_at_capacity([res])


def test_full_max_spawn_is_recorded(kanban_home, all_assignees_spawnable):
    with kbc.connect() as conn:
        assert kb.claim_task(conn, kb.create_task(conn, title="busy", assignee="alice")) is not None
        kb.create_task(conn, title="waits", assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory([]), max_spawn=1)
    assert res.at_capacity == "max_spawn=1/1"


def test_under_cap_tick_is_not_at_capacity(kanban_home, all_assignees_spawnable):
    with kbc.connect() as conn:
        kb.create_task(conn, title="runs", assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory([]), max_in_progress=2)
    assert res.at_capacity is None
    assert not kbd.all_at_capacity([res])


def test_all_at_capacity_needs_every_ticked_board_capped():
    capped, free = kb.DispatchResult(), kb.DispatchResult()
    capped.at_capacity = "max_in_progress=6/6"
    locked = kb.DispatchResult()
    locked.skipped_locked = True
    assert kbd.all_at_capacity([capped, None, locked])
    assert not kbd.all_at_capacity([capped, free])
    assert not kbd.all_at_capacity([])
    assert not kbd.all_at_capacity([locked]), "no board actually ticked"


def test_all_at_capacity_ignores_boards_owned_by_another_dispatcher():
    capped = kb.DispatchResult(at_capacity="max_in_progress=16/16")
    owned = kb.DispatchResult(skipped_owned=True)
    assert kbd.all_at_capacity([capped, owned])
    assert not kbd.all_at_capacity([owned]), "no board actually ticked"
    assert not kbd.all_at_capacity([capped, owned, kb.DispatchResult()])


def _daemon_stderr(monkeypatch, capsys, res, ticks=8):
    import argparse
    from hermes_cli import kanban_ops

    monkeypatch.setattr(kbd, "has_spawnable_ready", lambda conn: True)

    def fake_run_daemon(*, on_tick, **_kw):
        for _ in range(ticks):
            on_tick(res)

    monkeypatch.setattr(kbd, "run_daemon", fake_run_daemon)
    assert kanban_ops._cmd_daemon(argparse.Namespace(force=True, interval=1, max=None, pidfile=None)) == 0
    return capsys.readouterr().err


def test_cli_daemon_does_not_warn_stuck_when_cap_is_full(kanban_home, monkeypatch, capsys):
    res = kb.DispatchResult()
    res.at_capacity = "max_in_progress=6/6"
    assert "dispatcher stuck" not in _daemon_stderr(monkeypatch, capsys, res)


def test_cli_daemon_still_warns_stuck_when_nothing_holds_spawns(kanban_home, monkeypatch, capsys):
    assert "dispatcher stuck" in _daemon_stderr(monkeypatch, capsys, kb.DispatchResult())


# ---------------------------------------------------------------------------
# 5. ``hermes kanban dispatch`` names a full cap instead of a bare "Spawned: 0" [daveey]
# ---------------------------------------------------------------------------


def _cli_dispatch_with_full_host_cap(monkeypatch, capsys, *, as_json):
    """Run ``_cmd_dispatch`` against a real board whose host cap (max_in_progress=2) is full."""
    import argparse
    from hermes_cli import kanban_ops

    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: {"kanban": {"max_in_progress": 2}},
    )
    with kbc.connect() as conn:
        for title in ("busy-1", "busy-2"):
            assert kb.claim_task(conn, kb.create_task(conn, title=title, assignee="alice")) is not None
        kb.create_task(conn, title="waits", assignee="alice")
    args = argparse.Namespace(dry_run=True, max=None, failure_limit=2, json=as_json)
    assert kanban_ops._cmd_dispatch(args) == 0
    return capsys.readouterr().out


def test_cli_dispatch_text_names_full_host_cap(kanban_home, all_assignees_spawnable, monkeypatch, capsys):
    out = _cli_dispatch_with_full_host_cap(monkeypatch, capsys, as_json=False)
    assert "Spawned:      0" in out
    assert "At capacity:  max_in_progress=2/2" in out


def test_cli_dispatch_json_names_full_host_cap(kanban_home, all_assignees_spawnable, monkeypatch, capsys):
    import json

    payload = json.loads(_cli_dispatch_with_full_host_cap(monkeypatch, capsys, as_json=True))
    assert payload["spawned"] == []
    assert payload["at_capacity"] == "max_in_progress=2/2"


def test_cli_dispatch_text_omits_capacity_line_under_cap(kanban_home, all_assignees_spawnable, monkeypatch, capsys):
    import argparse
    from hermes_cli import kanban_ops

    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {"max_in_progress": 2}})
    with kbc.connect() as conn:
        kb.create_task(conn, title="runs", assignee="alice")
    assert kanban_ops._cmd_dispatch(argparse.Namespace(dry_run=True, max=None, failure_limit=2, json=False)) == 0
    assert "At capacity" not in capsys.readouterr().out
