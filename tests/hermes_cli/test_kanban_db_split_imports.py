"""Every ``kanban_db_*`` split module imports first, in a fresh interpreter.

Regression (release run 40717630046, e2e-upgrade core): ``kanban_db_schedule`` imported
``kanban_db`` at the top, and ``kanban_db`` imports ``schedule_task`` back from it, so importing the
split module before ``kanban_db`` raised a circular ImportError. The Linux-only fresh-process e2e
caught it in release lanes only; this runs everywhere.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULES = sorted(f"hermes_cli.{p.stem}" for p in (ROOT / "hermes_cli").glob("kanban_db_*.py"))


@pytest.mark.parametrize("module", MODULES)
def test_split_module_imports_first(module):
    proc = subprocess.run([sys.executable, "-c", f"import {module}"], cwd=ROOT, capture_output=True,
                          text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr[-2000:]
