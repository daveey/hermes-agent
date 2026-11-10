"""[daveey] An explicit ``HERMES_LAUNCHD_LABEL`` names the launchd job whatever its prefix (gateway-64b702b67e).

The mini's gateways run under hand-written ``com.daveey.babybear.*`` plists. Below the job's first process
``XPC_SERVICE_NAME`` is ``0``, so the plist's ``EnvironmentVariables.HERMES_LAUNCHD_LABEL`` is the only
place the gateway can learn its job; the old ``ai.hermes`` prefix filter discarded it, the drain cap fell
back to the assumed 60 s and these 45 s ``ExitTimeOut`` jobs could be SIGKILLed mid-teardown.
The prefix filter stays on ``XPC_SERVICE_NAME``: launchd also exports app-coalition names
(``application.<bundle>…``) there for IDE integrated terminals, and those are not our job.
"""

from __future__ import annotations

import subprocess

from gateway.restart import (
    LAUNCHD_LABEL_ENV,
    launchd_job_label,
    launchd_service_label,
    read_launchd_exit_timeout_s,
)
from hermes_cli.stderr_timestamp import _child_launchd_label_env

BB_LABEL = "com.daveey.babybear.hermes"


def test_explicit_label_counts_whatever_its_prefix():
    env = {"XPC_SERVICE_NAME": "0", LAUNCHD_LABEL_ENV: BB_LABEL}
    assert launchd_job_label(env) == BB_LABEL
    assert launchd_service_label(env, platform="darwin") == BB_LABEL


def test_app_coalition_xpc_name_without_explicit_label_is_still_rejected():
    assert launchd_job_label({"XPC_SERVICE_NAME": "application.com.foo.12345"}) is None
    assert launchd_job_label({"XPC_SERVICE_NAME": "0"}) is None


def test_ai_hermes_xpc_name_still_counts_and_wins():
    env = {"XPC_SERVICE_NAME": "ai.hermes.gateway", LAUNCHD_LABEL_ENV: BB_LABEL}
    assert launchd_job_label(env) == "ai.hermes.gateway"


def test_app_coalition_xpc_name_falls_through_to_explicit_label():
    env = {"XPC_SERVICE_NAME": "application.com.foo.1", LAUNCHD_LABEL_ENV: BB_LABEL}
    assert launchd_job_label(env) == BB_LABEL


def test_blank_or_app_coalition_explicit_label_is_no_label():
    assert launchd_job_label({"XPC_SERVICE_NAME": "0", LAUNCHD_LABEL_ENV: "  "}) is None
    assert launchd_job_label({"XPC_SERVICE_NAME": "0", LAUNCHD_LABEL_ENV: "0"}) is None
    assert launchd_job_label({"XPC_SERVICE_NAME": "0", LAUNCHD_LABEL_ENV: "application.com.x.1"}) is None


def test_explicit_label_reads_the_jobs_live_exit_timeout():
    seen = []

    def run(cmd, **_kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="\texit timeout = 45\n", stderr="")

    env = {"XPC_SERVICE_NAME": "0", LAUNCHD_LABEL_ENV: BB_LABEL}
    assert read_launchd_exit_timeout_s(environ=env, uid=501, run=run, platform="darwin") == 45.0
    assert seen and seen[0][-1] == f"gui/501/{BB_LABEL}"


def test_stderr_wrapper_forwards_explicit_label_whatever_its_prefix():
    assert _child_launchd_label_env({"XPC_SERVICE_NAME": "0", LAUNCHD_LABEL_ENV: BB_LABEL}) == {
        LAUNCHD_LABEL_ENV: BB_LABEL
    }
    assert _child_launchd_label_env({"XPC_SERVICE_NAME": "application.com.foo.1"}) == {}
    assert _child_launchd_label_env({"XPC_SERVICE_NAME": "0", LAUNCHD_LABEL_ENV: "application.com.x.1"}) == {}
