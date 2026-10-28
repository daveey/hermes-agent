"""Replay ci.yaml's concurrency group (and every called workflow's) per event.

GitHub keeps at most one running and one pending run per concurrency group; a
new pending run supersedes the older pending one, and ``cancel-in-progress``
additionally cancels the running one. These tests evaluate the real
expressions from the workflow files with ``tests.ci._gha_expr`` for push,
dispatch, PR and release events and assert the coalescing rules:

* pushes to main share ``ci-main`` and never cancel a running push;
* dispatches on one ref share one group and never cancel a running dispatch
  (so dispatches cannot stack one full suite each on the hermes-hub pool);
* a dispatch and a push never share a group, and neither does a release;
* PRs collapse per PR with cancel-in-progress;
* two caller runs that can be in flight together never put a called workflow
  in the same group (which would let one cancel the other's in-flight jobs).
"""

from __future__ import annotations

import itertools
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from tests.ci import _gha_expr as gha

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
_YAML = YAML(typ="safe")


@cache
def _load(name: str) -> dict[str, Any]:
    return _YAML.load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _ctx(event: str, ref: str, run_id: int, *, inputs: dict[str, Any] | None = None,
         pr: int | None = None) -> dict[str, Any]:
    github: dict[str, Any] = {
        "event_name": event,
        "ref": ref,
        "ref_type": "tag" if ref.startswith("refs/tags/") else "branch",
        "run_id": run_id,
        "event": {"pull_request": {"number": pr}} if pr is not None else {},
    }
    return {"github": github, "inputs": inputs or {}}


def _group(workflow: str, ctx: dict[str, Any]) -> tuple[str, bool]:
    conc = _load(workflow)["concurrency"]
    group = gha.render(conc["group"], ctx)
    cancel = gha.truthy(gha.render(conc.get("cancel-in-progress", False), ctx))
    return str(group), cancel


def _ci(ctx: dict[str, Any]) -> tuple[str, bool]:
    return _group("ci.yaml", ctx)


MAIN = "refs/heads/main"
BRANCH = "refs/heads/fix/some-branch"

PUSH_A = _ctx("push", MAIN, 101)
PUSH_B = _ctx("push", MAIN, 102)
DISPATCH_MAIN_A = _ctx("workflow_dispatch", MAIN, 201, inputs={"release": False, "strict_acceptance": ""})
DISPATCH_MAIN_B = _ctx("workflow_dispatch", MAIN, 202, inputs={"release": False, "strict_acceptance": ""})
DISPATCH_MAIN_STRICT = _ctx("workflow_dispatch", MAIN, 203, inputs={"release": False, "strict_acceptance": "1"})
DISPATCH_BRANCH = _ctx("workflow_dispatch", BRANCH, 204, inputs={"release": False, "strict_acceptance": ""})
RELEASE_A = _ctx("workflow_dispatch", MAIN, 301, inputs={"release": True, "strict_acceptance": ""})
RELEASE_B = _ctx("workflow_call", MAIN, 302, inputs={"release": True})
PR_7_A = _ctx("pull_request", "refs/pull/7/merge", 401, pr=7)
PR_7_B = _ctx("pull_request", "refs/pull/7/merge", 402, pr=7)
PR_8 = _ctx("pull_request", "refs/pull/8/merge", 403, pr=8)


def test_main_pushes_coalesce_without_cancelling():
    assert _ci(PUSH_A) == _ci(PUSH_B) == ("ci-main", False)


def test_dispatches_on_one_ref_coalesce_without_cancelling():
    group_a, cancel_a = _ci(DISPATCH_MAIN_A)
    group_b, cancel_b = _ci(DISPATCH_MAIN_B)
    assert group_a == group_b, "two dispatches on main must share one group (one runs, newest waits)"
    assert not cancel_a and not cancel_b, "a running dispatch is never cancelled"
    assert str(DISPATCH_MAIN_A["github"]["run_id"]) not in group_a


def test_dispatch_never_shares_a_group_with_push_release_or_other_ref():
    dispatch, _ = _ci(DISPATCH_MAIN_A)
    assert dispatch != _ci(PUSH_A)[0]
    assert dispatch != _ci(DISPATCH_BRANCH)[0]
    assert dispatch != _ci(DISPATCH_MAIN_STRICT)[0], "a strict-acceptance dispatch must not supersede a plain gate"
    assert dispatch != _ci(RELEASE_A)[0]


def test_release_runs_never_coalesce_or_cancel():
    assert _ci(RELEASE_A) == ("ci-301", False)
    assert _ci(RELEASE_B) == ("ci-302", False)


def test_pull_requests_collapse_per_pr_and_cancel():
    assert _ci(PR_7_A) == _ci(PR_7_B) == ("ci-7", True)
    assert _ci(PR_8)[0] != _ci(PR_7_A)[0]


def _callees() -> list[str]:
    names = set()
    for job in _load("ci.yaml")["jobs"].values():
        uses = job.get("uses", "")
        if uses.startswith("./.github/workflows/"):
            names.add(uses.rsplit("/", 1)[1])
    return sorted(n for n in names if "concurrency" in _load(n))


CONCURRENT_CALLERS = {
    "push-main": PUSH_A,
    "dispatch-main": DISPATCH_MAIN_A,
    "dispatch-main-strict": DISPATCH_MAIN_STRICT,
    "dispatch-branch": DISPATCH_BRANCH,
    "release": RELEASE_A,
    "pr-7": PR_7_A,
    "pr-8": PR_8,
}


def test_concurrent_callers_are_in_distinct_ci_groups():
    groups = {name: _ci(ctx)[0] for name, ctx in CONCURRENT_CALLERS.items()}
    assert len(set(groups.values())) == len(groups), groups


@pytest.mark.parametrize("callee", _callees())
def test_concurrent_callers_never_share_a_callee_group(callee):
    """Callers in different ci.yaml groups can run at once; their children must not collide."""
    rendered = {name: _group(callee, ctx)[0] for name, ctx in CONCURRENT_CALLERS.items()}
    for (a, ga), (b, gb) in itertools.combinations(rendered.items(), 2):
        assert ga != gb, f"{callee}: {a} and {b} share child group {ga!r}"


def test_callee_set_covers_the_hub_workflows():
    callees = _callees()
    assert "tests.yml" in callees and "lint.yml" in callees, callees


def test_format_matches_actions_semantics():
    assert gha.evaluate("format('dispatch-{0}-{1}', 'a', '')", {}) == "dispatch-a-"
    assert gha.evaluate("format('{{x}} {0}', 3)", {}) == "{x} 3"


# docker.yml owns its own push/PR triggers and is also called by
# stable-release.yml with a release-phase input.
def _docker(ctx: dict[str, Any]) -> tuple[str, bool]:
    return _group("docker.yml", ctx)


DOCKER_PUSH_A = _ctx("push", MAIN, 501)
DOCKER_PUSH_B = _ctx("push", MAIN, 502)
DOCKER_DISPATCH_MAIN = _ctx("workflow_dispatch", MAIN, 503)
DOCKER_RELEASE_TEST = _ctx("workflow_call", MAIN, 601, inputs={"release-phase": "test"})
DOCKER_RELEASE_PUBLISH = _ctx("workflow_call", MAIN, 601, inputs={"release-phase": "publish"})
DOCKER_RELEASE_OTHER = _ctx("workflow_call", MAIN, 602, inputs={"release-phase": "test"})
DOCKER_RELEASE_TAG = _ctx("workflow_call", "refs/tags/v1.2.3", 603, inputs={"release-phase": "test"})


def test_docker_main_pushes_coalesce_without_cancelling():
    """Main pushes keep one running + newest pending; a running publish is never cancelled."""
    group_a, cancel_a = _docker(DOCKER_PUSH_A)
    group_b, cancel_b = _docker(DOCKER_PUSH_B)
    assert group_a == group_b == "docker-refs/heads/main-standalone"
    assert not cancel_a and not cancel_b
    assert _docker(DOCKER_DISPATCH_MAIN) == (group_a, False)


def test_docker_release_calls_keep_per_run_groups_apart_from_main_pushes():
    test_group, test_cancel = _docker(DOCKER_RELEASE_TEST)
    publish_group, _ = _docker(DOCKER_RELEASE_PUBLISH)
    assert test_group == "docker-601-test" and not test_cancel
    assert publish_group == "docker-601-publish", "test and publish calls of one release must not collide"
    assert _docker(DOCKER_RELEASE_OTHER)[0] != test_group, "two releases never supersede each other"
    main_push = _docker(DOCKER_PUSH_A)[0]
    for ctx in (DOCKER_RELEASE_TEST, DOCKER_RELEASE_PUBLISH, DOCKER_RELEASE_OTHER, DOCKER_RELEASE_TAG):
        assert _docker(ctx)[0] != main_push, "a main push must never supersede a release call"


def test_docker_pull_requests_collapse_per_pr_and_cancel():
    assert _docker(PR_7_A) == _docker(PR_7_B) == ("docker-7-standalone", True)
    assert _docker(PR_8)[0] != _docker(PR_7_A)[0]
