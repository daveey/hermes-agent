"""Production card acquisition -> stored canonical outcome -> list-row serialization.

Only the external card inventory is doubled. All stores are disposable; no live reads.
"""
import importlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch


def sample():
    importlib.import_module("hermes_bootstrap")  # this checkout's committed dependency environment
    import hermes_state_outcome as outcome
    from hermes_cli import kanban_session_activity as activity
    from hermes_state import SessionDB

    rows = {}
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HERMES_HOME": directory}):
        db = SessionDB(db_path=Path(directory) / "state.db")
        try:
            db.create_session("chat", source="desktop")
            for name, cards in {
                "queued_ci": [{"id": "ci", "status": "scheduled", "wait": {
                    "wait_kind": "ci", "wait_state": "external_running", "external": {"job": "queued"}}}],
                "unknown": [{"id": "ci", "status": "scheduled", "stale": True}],
                "empty": [],
            }.items():
                with (patch.object(activity, "session_outcome_tasks", return_value=cards),
                      patch.object(activity, "active_task_counts", return_value={}),
                      patch.object(activity, "blocked_task_counts", return_value={})):
                    outcome.refresh_outcome("chat", "card_change", db=db)
                row = dict(db._read_one("SELECT id,outcome_json FROM sessions WHERE id='chat'"))
                rows[name] = outcome.stamp_outcome_rows([dict(row, profile="default")])[0]
        finally:
            db.close()
    return rows


if __name__ == "__main__":
    print(json.dumps(sample()))
