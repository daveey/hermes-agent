"""Real non-launch-profile process producers shared by Python and renderer regression tests."""
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def sample():
    from hermes_cli import kanban_execution_observations as keo
    from hermes_cli import process_session_activity as psa
    from hermes_state import SessionDB
    from tools.process_registry import ProcessRegistry, ProcessSession
    from tui_gateway import launch_profile_policy, server
    import tools.process_registry as pr

    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        work = home / "profiles" / "work"
        work.mkdir(parents=True)
        (work / "config.yaml").write_text("{}\n")
        db = SessionDB(db_path=work / "state.db")
        db.create_session("owner", source="desktop")
        launch_db = SessionDB(db_path=home / "state.db")
        registry = ProcessRegistry()
        frames, arrived = [], threading.Event()

        def write(frame):
            frames.append(frame)
            arrived.set()
            return True

        transport = SimpleNamespace(write=write)
        owner = {"session_key": "owner", "history": [], "transport": transport, "profile_home": work}
        child = subprocess.Popen([sys.executable, "-c", "import threading; threading.Event().wait(60)"])
        try:
            registry._running["proc-a"] = ProcessSession(
                id="proc-a", command="wait", session_key="owner", parent_session_id="owner", pid=child.pid,
                started_at=time.time(), host_start_time=ProcessRegistry._safe_host_start_time(child.pid))
            with (patch.dict("os.environ", {"HERMES_HOME": str(home)}),
                  patch.object(pr, "process_registry", registry),
                  patch.object(keo, "local_scope", return_value=("edge", "default")),
                  patch.object(server, "_sessions", {"ui-owner": owner}),
                  patch.object(server, "_hermes_home", home),
                  patch.object(server, "_served_profile_homes", {work}),
                  patch.object(server, "_get_db", return_value=launch_db),
                  patch.object(launch_profile_policy, "_snapshot", None)):
                psa._reset_cache_for_tests()
                row = psa.annotate_background_activity([{"id": "owner", "profile": "work"}])[0]
                roster = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "subagent.roster",
                                                "params": {"profile": "work"}})
                response = server.dispatch({"id": 2, "method": "process.list", "params": {"session_id": "ui-owner"}},
                                           transport=transport)
                if response is None:
                    assert arrived.wait(10), "process.list did not answer"
                    response = frames[-1]
                assert "error" not in response, response
                assert "error" not in roster, roster
                return {"row": row, "roster": roster["result"]["sessions"],
                        "processes": response["result"]["processes"], "now": time.time()}
        finally:
            child.kill()
            child.wait()
            db.close()
            launch_db.close()
            psa._reset_cache_for_tests()


if __name__ == "__main__":
    sys.__stdout__.write(json.dumps(sample()) + "\n")
