"""Native memory regressions must execute on Darwin, not skip on Linux."""
from pathlib import Path

import hermes_yaml

ROOT = Path(__file__).resolve().parents[2]


def test_native_memory_lane_routes_and_executes_real_tests():
    workflow = hermes_yaml.safe_load((ROOT / ".github/workflows/native-memory-macos.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    paths = triggers["pull_request"]["paths"]
    for path in ("hermes_cli/machine_memory_darwin.py", "hermes_cli/machine_metrics.py",
                 "tests/hermes_cli/test_machine_memory_darwin.py", "tests/hermes_cli/test_machine_memory_nofork.py"):
        assert path in paths
    assert "workflow_dispatch" in triggers
    assert triggers["push"]["branches"] == ["main"]
    job = workflow["jobs"]["native-memory"]
    assert job["runs-on"] == ["self-hosted", "macOS", "hermes-state"]
    assert "github.event.pull_request.head.repo.full_name == github.repository" in job["if"]
    assert workflow["permissions"] == {"contents": "read"}
    checkout, run = job["steps"]
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.head.sha || github.sha }}"
    command = run["run"]
    assert "HERMES_HOME" in command and "mktemp -d" in command
    assert "taskpolicy -c utility scripts/run_tests.sh" in command
    for test in ("test_machine_memory_darwin.py", "test_machine_memory_nofork.py"):
        assert f"tests/hermes_cli/{test}" in command
    assert "sys.platform == 'darwin'" in command
    assert "--file-retries 0" in command
