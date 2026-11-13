"""Baby Bear marker narrowing: a BB skill, repo or Linux-only CI card is not Mac work. [daveey]

Moved unchanged from test_kanban_placement.py, which is at its file-size cap.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_placement as kp
from tests.hermes_cli.test_kanban_placement import ROUTE, decide


def test_babybear_skill_or_repo_in_the_body_does_not_pin_the_card():
    # t_01a18329fe89fefb / t_01a18491e6b20fc9: metta disk checks kept on the MacBook by a BB skill name.
    d = decide({"title": "Disk H4: metta4 silent 81 h (unreachable, 12 runners offline)",
                "body": "Skills: babybear-ship-and-ops, five-whys.\nRemove the worktree ~/code/babybear-h4 "
                        "once daveey/babybear PR 1823 has merged."})
    assert d["where"] == "hub", d
    # Baby Bear named as the subject of the title still pins it.
    d = decide({"title": "Baby Bear voice listening regressed", "body": "x"})
    assert d["where"] == "local" and "baby bear" in d["why"], d


# t_01a188a358643778 (David-MBP board, exact title and body): placed local by "mentions 'babybear'"
# and claimed by the MacBook, though the whole job is a Linux CI workflow on self-hosted runners.
BB_LINUX_CI_TITLE = ("babybear: move linux-pod-release `smoke` job off GitHub-hosted ubuntu-24.04 "
                     "(Actions billing-blocked)")
BB_LINUX_CI_BODY = (
    "GitHub-hosted Actions on daveey's private repos have been refused for billing since 2026-10-17 "
    "(ticket https://support.github.com/ticket/personal/0/4874098, still no reply). Re-verified "
    "2026-10-29 17:06 UTC: rerun of https://github.com/daveey/babybear/actions/runs/40905061318 smoke "
    "job refused at dispatch.\n\nThe ONLY remaining hosted job in daveey/babybear is `smoke` in "
    ".github/workflows/linux-pod-release.yml (runs-on: ubuntu-24.04). Every other job already targets "
    "self-hosted labels.\n\nDo:\n- Move `smoke` to `[self-hosted, Linux, X64, bb-linux]`, running "
    "pods/linux/tools/smoke_test.sh inside a privileged ubuntu:24.04 container booted with /sbin/init "
    "(the script header says it supports this). Must not install/purge packages on the runner host "
    "itself; clean up the container always.\n- Check the bb-linux runners (metta0*/metta1*) can run "
    "privileged docker; if not, pick another self-hosted option and say why.\n\nAcceptance:\n- PR "
    "merged to main (standing approval) with CI green.\n- A real linux-pod-release run "
    "(workflow_dispatch or rerun of an existing release) shows `smoke` executing on a self-hosted "
    "runner and passing; link the run.\n- `grep -rn runs-on .github/workflows` shows no GitHub-hosted "
    "labels left.")


def test_babybear_title_with_linux_ci_only_work_goes_to_the_target():
    d = decide({"title": BB_LINUX_CI_TITLE, "body": BB_LINUX_CI_BODY, "assignee": "default",
                "requires": ["terminal", "file", "git", "kanban"]}, carries=True)
    assert d["where"] == "hub", d
    d = decide({"title": "Baby Bear: linux pod OTA fails on ubuntu runners", "body": "x"})
    assert d["where"] == "hub", d


@pytest.mark.parametrize("title,body", [
    # Apple-only work under a Baby Bear title stays here even when Linux is also named.
    ("babybear: Xcode archive fails after linux-pod-release split", "runs-on: [self-hosted, Linux]"),
    ("babybear: iOS app crashes in the simulator", "Linux pods are fine."),
    ("babybear: TestFlight upload stuck", "The linux-pod-release workflow is green."),
    ("babybear: release signing identity expired", "docker smoke on ubuntu passes; signing identity "
                                                   "for the iOS app expired"),
    ("babybear: macOS client release", ".github/workflows/linux-pod-release.yml is unaffected"),
    ("babybear: provisioning profile for the iPhone build", "ubuntu runners unaffected"),
    # Review counterexamples: Mac CI on self-hosted runners and Mac-client work.
    ("babybear: xcodebuild archive fails on self-hosted runner",
     "runs-on: [self-hosted, bb-mac]; xcodebuild -scheme BabyBear archive; linux jobs green"),
    ("babybear: fix fastlane match step", "linux runners fine; fastlane match + xcrun altool"),
    ("Baby Bear: Mac app wake word broken", "linux pods are fine; only the Mac client"),
    ("babybear: CoreML model in the watch app", "docker image for training on linux is fine"),
    # A device reached through the mini.
    ("Baby Bear: restart the pod runtime on the mini", "the linux pods are behind the mini"),
    # No Linux evidence at all: the product title still pins (unchanged).
    ("Baby Bear voice listening regressed", "x"),
])
def test_babybear_title_with_apple_or_device_work_stays_local(title, body):
    d = decide({"title": title, "body": body})
    assert d["where"] == "local" and d["why"].startswith("local marker"), d


def test_replaced_marker_list_keeps_product_name_pinning():
    # A caller who replaces local_markers owns the list: no Linux-context exemption is applied.
    cfg = {**ROUTE, "local_markers": [kp.BABYBEAR_MARKER]}
    d = decide({"title": BB_LINUX_CI_TITLE, "body": BB_LINUX_CI_BODY}, cfg=cfg)
    assert d["where"] == "local", d
