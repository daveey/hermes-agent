"""Queue-fairness capture, end to end: a real ``dispatch_once`` on a real board database, journaled by the
SQLite triggers, drained to segments, sealed, and read back by the operator CLI. [daveey]

Worker launch is the only test adapter (``spawn_fn``); selection, claim and every row write are production
code. The clock is the database host's, so waits here are a few milliseconds: the assertions are about which
episodes exist, how each ended, and that the capture is complete or says exactly why it is not.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_fairness_journal as journal
from hermes_cli import kanban_fairness_metrics as metrics
from hermes_cli import kanban_fairness_store as store
from hermes_cli.kanban_admission_score import ALARM_KEY_PREFIXES

_SPEC = importlib.util.spec_from_file_location(
    "kanban_fairness_cli", Path(__file__).resolve().parents[2] / "scripts" / "kanban_fairness.py")
cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cli)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _configure(home)
    kb.init_db()
    return home


def _configure(home):
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    from hermes_cli.kanban_model_assignment import _assignee_config
    (home / "config.yaml").write_text("kanban:\n  dispatch_profiles: [worker-a]\n", encoding="utf-8")
    profile = home / "profiles" / "worker-a"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "config.yaml").write_text(_managed_yaml(), encoding="utf-8")
    config = _assignee_config("worker-a")
    assert config is not None and config["model_routing"]["enabled"] is True


def _spawn(task, _workspace):
    if task.title == "launch fails":
        raise RuntimeError("no worker")
    return 4242


def _lifecycle(conn):
    """Everything dispatch-relevant about the board, without the evidence tables."""
    tasks = conn.execute("SELECT title, status, priority, assignee, claim_lock IS NOT NULL FROM tasks "
                         "ORDER BY title").fetchall()
    events = conn.execute("SELECT t.title, e.kind FROM task_events e JOIN tasks t ON t.id = e.task_id "
                          "ORDER BY e.id").fetchall()
    per_card: dict = {}  # equal-score cards are visited in task-id order, which is random per run
    for title, kind in events:
        per_card.setdefault(title, []).append(kind)
    return [tuple(r) for r in tasks], per_card


def _scenario(conn):
    """Two priorities, a dependency, an alarm, a failed launch, a card nobody may claim."""
    ids = {
        "backlog": kb.create_task(conn, title="backlog p0", assignee="worker-a"),
        "urgent": kb.create_task(conn, title="urgent p1000", assignee="worker-a", priority=1000),
        "alarm": kb.create_task(conn, title="alarm", assignee="worker-a",
                                idempotency_key="worker-claim-watch:w9"),
        "fails": kb.create_task(conn, title="launch fails", assignee="worker-a"),
        "foreign": kb.create_task(conn, title="not ours", assignee="worker-b"),
    }
    ids["child"] = kb.create_task(conn, title="child", assignee="worker-a", parents=[ids["backlog"]])
    return ids


def test_dispatch_to_report(kanban_home, all_assignees_spawnable, tmp_path, capsys):
    out = tmp_path / "evidence"
    db = kanban_home / "kanban.db"
    with kbc.connect_closing() as conn:
        early = kb.create_task(conn, title="opening backlog", assignee="worker-b")
    assert cli.main(["capture", "arm", "--out", str(out), "--capture-id", "t1", "--db", str(db),
                     "--unclaimed-exit", "elapsed_ready"]) == 0
    with kbc.connect_closing() as conn:
        ids = _scenario(conn)
        res = kbd.dispatch_once(conn, spawn_fn=_spawn)
        assert {ids["backlog"], ids["urgent"], ids["alarm"]} <= {s[0] for s in res.spawned}
        assert kb.get_task(conn, ids["child"]).status == "todo"
        kb.complete_task(conn, ids["backlog"], result="done")
        kbd.dispatch_once(conn, spawn_fn=_spawn)  # the child is ready now and gets claimed
        kb.archive_task(conn, ids["foreign"])
    assert cli.main(["capture", "drain", "--out", str(out)]) == 0
    assert cli.main(["capture", "seal", "--out", str(out)]) == 0
    assert cli.main(["validate", "--input", str(out)]) == 2
    capsys.readouterr()

    capture = store.load(out)
    expected_gaps = {"source_identity_unknown", "clock_unknown"}
    assert {g["code"] for g in capture["gaps"]} == expected_gaps
    eps = metrics.episodes(capture["records"], armed_s=capture["manifest"]["window"]["armed_ms"] / 1000,
                           err=0)
    by_task = {}
    for ep in eps:
        by_task.setdefault(ep.task_id, []).append(ep)
    assert [e.end for e in by_task[ids["backlog"]]] == ["claimed"]
    assert [e.end for e in by_task[ids["urgent"]]] == ["claimed"]
    assert [e.end for e in by_task[ids["child"]]] == ["claimed"]  # never ready before its parent finished
    assert by_task[ids["urgent"]][0].priority == 1000
    assert by_task[ids["alarm"]][0].key == "worker-claim-watch:w9"
    assert [e.end for e in by_task[ids["foreign"]]] == ["left:archived"]
    assert [(e.end, e.opening) for e in by_task[early]] == [("open", True)]
    # A failed launch ends the first wait at the claim; the card's return to ready is a new episode.
    assert by_task[ids["fails"]][0].end == "claimed"
    assert len(by_task[ids["fails"]]) >= 2

    report = tmp_path / "live.json"
    code = cli.main(["report", "--mode", "live", "--input", str(out), "--output", str(report)])
    verdict = json.loads(report.read_text())
    assert verdict["coverage"]["complete"] is False
    assert verdict["cohorts"]["all"]["opening_backlog"] == 1
    gates = verdict["gates"]
    assert gates["alarm_wait"]["status"] == "INDETERMINATE" and gates["alarm_wait"]["episodes"] == 1
    assert gates["max_wait_while_claims"]["status"] == "INDETERMINATE"
    # No owner journal was supplied: that gate cannot pass, so neither can the capture.
    assert gates["owner_wait"]["status"] == "INDETERMINATE"
    assert (verdict["overall"], code) == ("INDETERMINATE", 2)
    assert verdict["policy"]["alarm_prefixes"] == list(ALARM_KEY_PREFIXES)


def test_capture_does_not_change_what_the_dispatcher_does(
        kanban_home, all_assignees_spawnable, tmp_path, monkeypatch):
    def run(home, armed):
        monkeypatch.setenv("HERMES_HOME", str(home))
        _configure(home)
        kb.init_db()
        with kbc.connect_closing() as conn:
            if armed:
                journal.arm(conn, "identity")
            ids = _scenario(conn)
            kbd.dispatch_once(conn, spawn_fn=_spawn)
            kb.complete_task(conn, ids["backlog"], result="done")
            kbd.dispatch_once(conn, spawn_fn=_spawn)
            life = _lifecycle(conn)
            assert "claimed" in life[1]["backlog p0"] and "claimed" in life[1]["child"]
            return life

    plain_home = tmp_path / "plain"
    plain_home.mkdir()
    assert run(kanban_home, armed=True) == run(plain_home, armed=False)


def test_unsealed_capture_is_incomplete(kanban_home, tmp_path, capsys):
    out = tmp_path / "evidence"
    assert cli.main(["capture", "arm", "--out", str(out), "--capture-id", "t2",
                     "--db", str(kanban_home / "kanban.db")]) == 0
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="x", assignee="worker-a")
    assert cli.main(["capture", "drain", "--out", str(out)]) == 0
    assert cli.main(["validate", "--input", str(out)]) == 2
    assert "unsealed" in capsys.readouterr().out
