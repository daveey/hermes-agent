"""Core CI runs tests/origin_transport/ against a pinned hermes-hub courier and fails on any skip.

The composed forwarding tests skip unless pytest gets ``--courier-client``; before this lane only
hermes-hub's core-observation job passed it, so a core change that broke forwarding
(33922f7e, 2026-11-23) stayed green in core. hermes-hub is private and its checkout needs a
read-only deploy key; ci.yaml runs PR-controlled code and must stay secret-free, so the lane is
its own workflow whose key is an environment secret resolved only on trusted events.
"""
from __future__ import annotations

import pathlib
import re

from ruamel.yaml import YAML

_YAML = YAML(typ="safe")
ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "origin-transport.yml"
# The env hermes-hub's core-observation job sets for this suite (client-cli.yml): a worker's board
# pin and session must never become the suite default.
HUB_CANARY_ENV = {
    "HERMES_KANBAN_DB": "/tmp/not-the-owner-board.db",
    "HERMES_KANBAN_HOME": "/tmp/not-the-owner-home",
    "HERMES_DELEGATED_CHILD_CONTEXT": "/tmp/not-the-owner-home",
    "HERMES_DELEGATED_CHILD_OWNER": "worker-must-not-own-the-suite",
    "HERMES_SESSION_ID": "worker-session-must-not-be-the-origin",
}


def _load(path: pathlib.Path) -> dict:
    return _YAML.load(path.read_text(encoding="utf-8")) or {}


def _job() -> dict:
    jobs = _load(WORKFLOW)["jobs"]
    assert list(jobs) == ["composed"], list(jobs)
    return jobs["composed"]


def _steps() -> list[dict]:
    return _job()["steps"]


def _pin() -> str:
    pin = str(_load(WORKFLOW)["env"]["HERMES_HUB_PIN"])
    assert re.fullmatch(r"[0-9a-f]{40}", pin), pin
    return pin


def test_hub_checkout_is_pinned_read_only_and_never_persisted():
    hub = [s for s in _steps() if (s.get("with") or {}).get("repository") == "daveey/hermes-hub"]
    assert [h["with"]["path"] for h in hub] == [".composed-hub", ".hub-main"], hub
    for step in hub:
        assert step["with"]["ssh-key"] == "${{ secrets.HERMES_HUB_READ_KEY }}"
        assert step["with"]["persist-credentials"] is False
    # The pin, or the ref a manual dispatch names (its default is the pin).
    assert hub[0]["with"]["ref"] == "${{ inputs.hub_ref || env.HERMES_HUB_PIN }}"
    data = _load(WORKFLOW)
    triggers = data.get("on") or data.get(True)
    assert triggers["workflow_dispatch"]["inputs"]["hub_ref"]["default"] == _pin()


def test_no_checkout_persists_credentials():
    checkouts = [s for s in _steps() if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert len(checkouts) == 3
    assert all((s.get("with") or {}).get("persist-credentials") is False for s in checkouts)


def test_a_weekly_run_fails_when_the_hub_client_moved_past_the_pin():
    data = _load(WORKFLOW)
    triggers = data.get("on") or data.get(True)
    assert [c["cron"] for c in triggers["schedule"]] and len(triggers["schedule"]) == 1
    main = next(s for s in _steps() if (s.get("with") or {}).get("path") == ".hub-main")
    assert main["if"] == "github.event_name == 'schedule'"
    assert main["with"]["ref"] == "main" and main["with"]["fetch-depth"] == 0
    stale = [s for s in _steps() if "client/hermes_hub.py" in str(s.get("run", ""))]
    assert len(stale) == 1 and stale[0]["if"] == "github.event_name == 'schedule'"
    script = stale[0]["run"]
    assert "HERMES_HUB_PIN" in script and "merge-base --is-ancestor" in script and "exit 1" in script and "::error::" in script


def test_the_whole_directory_runs_with_the_courier_client_and_the_hub_canary_env():
    runs = [s for s in _steps() if "tests/origin_transport/" in str(s.get("run", ""))]
    assert len(runs) == 1, runs
    step = runs[0]
    assert '--courier-client="$COURIER"' in step["run"]
    assert step["env"]["COURIER"].endswith("/client/hermes_hub.py")
    for key, value in HUB_CANARY_ENV.items():
        assert step["env"].get(key) == value, key
    # The canonical runner starts each file from `env -i`, which would drop the canary env, and
    # scripts/run-in-hermes-env would set the environment up again: setup-pm's interpreter runs it.
    assert "run_tests.sh" not in step["run"] and "run-in-hermes-env" not in step["run"]
    assert step["run"].lstrip().startswith('"$HERMES_PYTHON" -m pytest')
    assert "--junitxml=" in step["run"]


def test_a_skip_or_an_empty_run_fails_the_lane():
    checks = [s for s in _steps() if "check_origin_transport_junit" in str(s.get("run", ""))]
    assert len(checks) == 1, checks
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_origin_transport_junit", ROOT / "scripts" / "ci" / "check_origin_transport_junit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def suite(tests, skipped=0, failures=0, errors=0):
        return (f'<testsuites><testsuite name="pytest" tests="{tests}" skipped="{skipped}" '
                f'failures="{failures}" errors="{errors}"/></testsuites>')

    assert mod.verdict(suite(8)) == (True, "8 passed")
    assert mod.verdict(suite(8, skipped=1))[0] is False
    assert mod.verdict(suite(0))[0] is False
    assert mod.verdict(suite(8, failures=1))[0] is False
    assert mod.verdict(suite(8, errors=1))[0] is False


def test_key_is_environment_scoped_on_trusted_events_and_ci_yaml_stays_secret_free():
    data = _load(WORKFLOW)
    triggers = data.get("on") or data.get(True)
    assert set(triggers) == {"push", "workflow_dispatch", "schedule"}, triggers
    assert triggers["push"]["branches"] == ["main"]
    assert _job()["environment"] == "hermes-hub-read"
    assert "HERMES_HUB_READ_KEY" not in (WORKFLOWS / "ci.yaml").read_text(encoding="utf-8")
    assert "origin-transport" not in (WORKFLOWS / "ci.yaml").read_text(encoding="utf-8")
    # A missing key fails loudly instead of skipping the checkout.
    guard = [s for s in _steps() if "HERMES_HUB_READ_KEY" in str(s.get("env", ""))]
    assert guard and "exit 1" in guard[0]["run"]
