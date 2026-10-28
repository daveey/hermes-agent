"""A pytest launched by a Kanban worker inherits HERMES_KANBAN_DB=<the live board> and friends. conftest must drop
them before any test module is imported, not only per test: collection-time code and subprocesses started early
would otherwise resolve the production board (hub incident 2026-10-28). [daveey]"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

#: The HERMES_KANBAN_* variables visible while this module was being collected (after conftest ran).
COLLECTION_TIME_KANBAN_ENV = sorted(k for k in os.environ if k.startswith("HERMES_KANBAN_"))
_PROBE = "HERMES_TEST_KANBAN_SCRUB_PROBE"


def test_collection_time_env_has_no_kanban_pins():
    if not os.environ.get(_PROBE):
        return  # meaningful only in the child below, which is started WITH the pins
    assert COLLECTION_TIME_KANBAN_ENV == []


def test_worker_kanban_pins_never_reach_collection(tmp_path):
    live = tmp_path / "live" / "kanban.db"
    env = {**os.environ, _PROBE: "1", "HERMES_KANBAN_DB": str(live), "HERMES_KANBAN_BOARD": "default",
           "HERMES_KANBAN_HOME": str(live.parent), "HERMES_KANBAN_TASK": "t_probe"}
    root = Path(__file__).resolve().parents[1]
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
         f"{Path(__file__).resolve()}::test_collection_time_env_has_no_kanban_pins"],
        cwd=root, env=env, capture_output=True, text=True, timeout=240, check=False)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]
    assert not live.exists(), "nothing may create the pinned board"
