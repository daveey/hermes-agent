"""Run the Python planner in the checkout configured for CI's detect job."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
import hermes_yaml as yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def detect_checkout(tmp_path_factory):
    checkout = tmp_path_factory.mktemp("detect-checkout") / "repo"
    config = yaml.safe_load((ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8"))
    steps = config["jobs"]["detect"]["steps"]
    options = next(step.get("with", {}) for step in steps
                   if step.get("uses", "").startswith("actions/checkout@"))
    subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout", str(ROOT), str(checkout)],
                   check=True, timeout=60)
    patterns = options.get("sparse-checkout")
    if patterns:
        cone = options.get("sparse-checkout-cone-mode", True)
        subprocess.run(["git", "-C", str(checkout), "sparse-checkout", "set",
                        "--cone" if cone else "--no-cone", "--stdin"],
                       input=patterns, text=True, encoding="utf-8", check=True, timeout=60)
    subprocess.run(["git", "-C", str(checkout), "checkout", "--quiet", "HEAD"], check=True, timeout=60)
    return checkout


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_detect_checkout_can_plan_python_tests(detect_checkout, tmp_path, event):
    output = tmp_path / "outputs.txt"
    changed = "tests/ci/test_affected_tests.py"
    args = ["--full", "push runs the full suite"] if event == "push" else ["--changed", changed]
    result = subprocess.run([sys.executable, "scripts/ci/affected_tests.py", *args,
                             "--github-output", str(output)], cwd=detect_checkout,
                            text=True, encoding="utf-8", capture_output=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["slices"] > 0
    if event == "push":
        assert plan["mode"] == "full"
    else:
        assert plan["mode"] == "affected"
        assert changed in plan["files"]
    assert "slices=" in output.read_text(encoding="utf-8")
