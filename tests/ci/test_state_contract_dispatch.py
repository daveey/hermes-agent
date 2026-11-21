"""scripts/ci/state_contract_dispatch.sh: which dispatches state-contract-dispatch.yml makes.

A fake `gh` first on PATH records every call; the script's selection logic is what is under test.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "state_contract_dispatch.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "state-contract-dispatch.yml"

REPO = "softmax-universe/daveey-hermes"
MODS_HEAD = "a" * 40
CORE_HEAD = "b" * 40
STATE_HEAD = "c" * 40
PUSH_SHA = "d" * 40
CANARY_TITLE = f"hermes-sync-tests upstream canary mods={MODS_HEAD} core={CORE_HEAD}"
PINS_YAML = (
    "env:\n  MODS_FIXTURE_SHA: " + "1" * 40 + "\n  CORE_FIXTURE_SHA: " + "2" * 40 + "\n"
)

FAKE_GH = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_GH_LOG"
case "$1 $2" in
  "workflow run") exit 0 ;;
  "run list") echo 987654321; exit 0 ;;
esac
if [ "$1" = api ]; then
  case "$2" in
    repos/*/commits/state) echo __STATE__ ;;
    repos/daveey/hermes-mods/commits/main) echo __MODS__ ;;
    repos/*/commits/main) echo __CORE__ ;;
    repos/*/contents/.github/workflows/hermes-sync-tests.yml*) printf '%s' "$FAKE_PINS_B64" ;;
    *) echo "unexpected api $2" >&2; exit 9 ;;
  esac
  exit 0
fi
echo "unexpected gh $*" >&2
exit 9
"""


def _run(tmp_path: Path, event: str, cron: str = "") -> tuple[list[str], str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(
        FAKE_GH.replace("__STATE__", STATE_HEAD).replace("__MODS__", MODS_HEAD).replace("__CORE__", CORE_HEAD)
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "gh.log"
    summary = tmp_path / "summary.md"
    import base64

    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_GH_LOG": str(log),
        "FAKE_PINS_B64": base64.b64encode(PINS_YAML.encode()).decode(),
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_SHA": PUSH_SHA,
        "GITHUB_EVENT_NAME": event,
        "GITHUB_STEP_SUMMARY": str(summary),
        "SCHEDULE_CRON": cron,
    }
    proc = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    calls = log.read_text().splitlines() if log.exists() else []
    return [c for c in calls if c.startswith("workflow run")], summary.read_text() if summary.exists() else ""


def test_push_dispatches_core_ref_only(tmp_path):
    runs, summary = _run(tmp_path, "push")
    assert runs == [
        f"workflow run bb-dev-tests.yml --repo {REPO} --ref state",
        f"workflow run hermes-sync-tests.yml --repo {REPO} --ref state -f core_ref={PUSH_SHA}",
    ]
    assert "mods_ref" not in " ".join(runs)
    assert PUSH_SHA in summary


def test_daily_schedule_is_unchanged(tmp_path):
    runs, _ = _run(tmp_path, "schedule", "17 11 * * *")
    assert runs == [
        f"workflow run bb-dev-tests.yml --repo {REPO} --ref state",
        f"workflow run hermes-sync-tests.yml --repo {REPO} --ref state",
    ]


def _assert_canary(runs: list[str], summary: str) -> None:
    assert runs == [
        f"workflow run hermes-sync-tests.yml --repo {REPO} --ref state -f mods_ref={MODS_HEAD} -f core_ref={CORE_HEAD}"
    ]
    assert f"mods main HEAD: `{MODS_HEAD}`" in summary
    assert f"core main HEAD: `{CORE_HEAD}`" in summary
    assert "MODS_FIXTURE_SHA: " + "1" * 40 in summary
    assert "CORE_FIXTURE_SHA: " + "2" * 40 in summary
    assert f"{REPO}/actions/runs/987654321" in summary


def test_hourly_schedule_dispatches_both_heads(tmp_path):
    _assert_canary(*_run(tmp_path, "schedule", "47 * * * *"))


def test_manual_dispatch_runs_the_canary(tmp_path):
    _assert_canary(*_run(tmp_path, "workflow_dispatch"))


def test_workflow_wires_script_and_crons():
    text = WORKFLOW.read_text()
    assert "- cron: '17 11 * * *'" in text
    assert re.search(r"- cron: '\d+ \* \* \* \*'", text), "hourly cron missing"
    assert "scripts/ci/state_contract_dispatch.sh" in text
    assert "SCHEDULE_CRON: ${{ github.event.schedule }}" in text
    assert "group: state-contract-dispatch-${{ github.event_name }}" in text
    assert "'scripts/ci/state_contract_dispatch.sh'" in text, "script change must trigger a push dispatch"
