"""scripts/ci/flaky_kanban.py run as a script imports hermes_cli from its own checkout.

The hub cron `ci-flaky-cards` launches ``<interpreter> scripts/ci/flaky_kanban.py``. On a managed (PM) install
that interpreter is a dependency-generation venv whose default ``hermes_cli`` is the generation's workspace
snapshot, rebuilt only on a dependency change. Script execution puts only scripts/ci on sys.path, so the stale
snapshot won: ``No module named 'hermes_cli.kanban_selfwork_producer'`` on every run once the producer path
imported a module newer than the snapshot (self-heal cron-d0f9004adc).

A decoy ``hermes_cli`` package on PYTHONPATH stands in for that snapshot: it ranks ahead of site-packages the
same way, and it records any import of itself.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "flaky_kanban.py"


def _decoy(tmp_path: Path) -> tuple[Path, Path]:
    pkg = tmp_path / "snapshot" / "hermes_cli"
    pkg.mkdir(parents=True)
    marker = tmp_path / "decoy-imported"
    (pkg / "__init__.py").write_text(
        "import os, pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text('stale snapshot hermes_cli imported')\n")
    return pkg.parent, marker


def _run(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess, Path]:
    snapshot, marker = _decoy(tmp_path)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONSAFEPATH")}
    env["PYTHONPATH"] = str(snapshot)
    proc = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=120, check=False)
    return proc, marker


def test_script_run_prefers_its_checkout_over_a_stale_hermes_cli(tmp_path):
    proc, marker = _run(tmp_path, "--help")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert not marker.exists(), "flaky_kanban.py imported a foreign hermes_cli ahead of its own checkout"


def test_script_run_resolves_the_selfwork_producer_from_its_checkout(tmp_path):
    """The exact import that failed live (flaky_selfwork.py), from the script's own process."""
    snapshot, marker = _decoy(tmp_path)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONSAFEPATH")}
    env["PYTHONPATH"] = str(snapshot)
    probe = (
        "import runpy, sys\n"
        f"sys.argv = [{str(SCRIPT)!r}, '--help']\n"
        f"sys.path[0] = {str(SCRIPT.parent)!r}\n"  # what `python scripts/ci/flaky_kanban.py` gets
        "try:\n"
        f"    runpy.run_path({str(SCRIPT)!r}, run_name='flaky_kanban_probe')\n"
        "finally:\n"
        "    from hermes_cli.kanban_selfwork_producer import validate\n"
        "    import hermes_cli\n"
        "    print(hermes_cli.__file__)\n")
    proc = subprocess.run([sys.executable, "-c", probe], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert Path(proc.stdout.strip().splitlines()[-1]).resolve() == ROOT / "hermes_cli" / "__init__.py"
    assert not marker.exists()
