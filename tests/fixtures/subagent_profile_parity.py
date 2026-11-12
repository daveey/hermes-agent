"""Actual persisted and registry subagent producers, shared by renderer/Python tests."""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def sample():
    from hermes_cli import subagent_session_activity as activity
    from hermes_cli.turn_process_execution_observations import registry_subagent
    from hermes_state import SessionDB

    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
        home = Path(directory)
        work = home / "profiles" / "work"
        work.mkdir(parents=True)
        db = SessionDB(work / "state.db")
        try:
            db.create_session("owner", source="cli")
            db.create_session("child-a", source="subagent", parent_session_id="owner")
            db.touch_session_activity("child-a", 1000)
            child = SimpleNamespace(session_id="child-a", _parent_session_id="owner",
                                    get_activity_summary=lambda: {"last_activity_ts": 1000})
            with (patch.dict(os.environ, {"HERMES_HOME": str(home)}),
                  patch.object(activity, "local_scope", return_value=("edge", "default")),
                  patch.object(activity.time, "time", return_value=1001)):
                activity._reset_cache_for_tests()
                row = activity.annotate_subagent_activity([{"id": "owner", "profile": "work"}])[0]
                registry = registry_subagent({"agent": child, "owner_agent_session_id": "owner",
                                              "started_at": 900, "status": "running"},
                                             gateway="edge", profile="work", now=1001)
                return {"row": row, "registry": registry, "now": 1001}
        finally:
            db.close()
            activity._reset_cache_for_tests()


if __name__ == "__main__":
    sys.__stdout__.write(json.dumps(sample()) + "\n")
