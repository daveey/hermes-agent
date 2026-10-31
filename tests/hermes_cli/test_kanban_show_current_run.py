"""The real show JSON must expose the owner-run identity consumed by nightly e2e."""

import json
from pathlib import Path

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def test_show_json_round_trips_current_run_id(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="acceptance owner")
        assert json.loads(kc.run_slash(f"show {tid} --json"))["task"]["current_run_id"] is None
        kb.recompute_ready(conn)
        task = kb.claim_task(conn, tid, claimer="test:owner")
        assert task is not None and task.current_run_id is not None
        run_id = task.current_run_id
    shown = json.loads(kc.run_slash(f"show {tid} --json"))
    assert shown["task"]["current_run_id"] == run_id
    assert any(r["id"] == run_id and r["status"] == "running" for r in shown["runs"])
