"""Real-process fixtures for process-registry pipe regressions."""

import os
import signal
import subprocess
import sys

import pytest


@pytest.fixture()
def orphan_pipe():
    """An exited direct child with stdout held by an explicitly released orphan."""
    # A sleep would eventually release stdout and let a broken blocking reader
    # pass. Keep the orphan alive until teardown, regardless of runner load.
    proc = subprocess.Popen(
        [sys.executable, "-c", """
import os
# orphan-pipe-fixture
if os.fork() == 0:
    os.read(0, 1)
    os._exit(0)
print('started', flush=True)
os._exit(0)
"""],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    try:
        # Child startup is not reader completion: establish that precondition
        # separately so descheduling the child doesn't consume the reader budget.
        assert proc.wait(timeout=60) == 0
        yield proc
    finally:
        # The known group ID survives reaping its leader; getpgid(proc.pid)
        # doesn't, and would leak the orphan after wait()/poll().
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=60)
        proc.stdin.close()
        proc.stdout.close()


START_MARKER = "__hermes_bg_start_fixture__"


@pytest.fixture(autouse=True)
def fixed_start_marker(monkeypatch):
    """A known start marker, so the login argv is comparable to ``bg_script()`` (autouse where imported)."""
    monkeypatch.setattr("tools.process_registry.new_start_marker", lambda: START_MARKER)


def bg_script():
    """Login-shell script spawn_local passes: init, print the start marker, then exec the foreground bash."""
    import shlex

    from tools.process_registry_bg_shell import _find_bash

    return ('set +m; _hc=$HERMES_BG_COMMAND; unset HERMES_BG_COMMAND; '
            f"printf '\\n%s\\n' {START_MARKER}; "
            f'exec {shlex.quote(_find_bash())} -c "$_hc"')
