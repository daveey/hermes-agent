"""The attach-only probe must use installed Node, not provision an unrelated PM runtime."""

from __future__ import annotations

import subprocess
import sys

from tests.e2e.core.security import test_attach_handshake as attach
from tests.e2e.core.security._helpers import hermetic_env, kill_group

pytestmark = attach.pytestmark
home = attach.home

_NO_PROVISION = """
import runpy, sys
import pm

def refuse_provision(*args, **kwargs):
    raise AssertionError(f"attach probe attempted PM provisioning: {args}")

pm.ensure = refuse_provision
sys.argv = ["hermes", *sys.argv[1:]]
runpy.run_module("hermes_cli.main", run_name="__main__")
"""


def test_attach_control_needs_no_pm_provisioning(home, monkeypatch):
    """Exercise the real CLI, lease holder and Node probe with PM acquisition forbidden."""
    def run_without_provision(argv, home, *, timeout, extra_env):
        proc = subprocess.Popen(
            [sys.executable, "-c", _NO_PROVISION, *argv],
            cwd=home, env=hermetic_env(home, extra_env),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        finally:
            kill_group(proc)
            proc.wait(timeout=10)
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)

    monkeypatch.setattr(attach, "run_hermes", run_without_provision)
    # Reuse all control assertions: exact socket, resume id and unchanged lease ownership.
    attach.test_genuine_owner_is_attached(home)
