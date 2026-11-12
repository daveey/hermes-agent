"""[daveey] Shared pieces of the throwaway runner trees that runner tests build.

The runner reads the account's worker test-budget policy
(``hermes_cli.worker_compute_capacity``) and its receipts helper
(``scripts/worker_test_budget.py``), so a probe tree needs both.
"""
from __future__ import annotations

import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def add_runner_imports(root: Path) -> None:
    """Copy the receipts helper and link the real top-level Python modules (no pytest config)."""
    shutil.copy2(REPO / "scripts" / "worker_test_budget.py", root / "scripts")
    for entry in REPO.iterdir():
        package = entry.is_dir() and (entry / "__init__.py").exists() and entry.name not in {"scripts", "tests"}
        module = entry.suffix == ".py" and entry.name != "conftest.py"
        if (package or module) and not (root / entry.name).exists():
            (root / entry.name).symlink_to(entry)
