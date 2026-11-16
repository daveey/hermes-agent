"""Opt-in pytest plugin: fail closed if the native Bash lane loses its interpreter.

Loaded with ``-p scripts.ci.bash32_guard`` in every canonical-runner pytest
process, after PM/bootstrap has composed PATH. Ordinary Linux tests do not load it.
"""
import platform
import shutil
import subprocess

import pytest


def pytest_sessionstart(session):
    """Check the interpreter actually visible to shell-executing fixtures."""
    if platform.system() != "Darwin" or shutil.which("bash") != "/bin/bash":
        raise pytest.UsageError("Bash 3.2 lane requires Darwin and PATH bash=/bin/bash")
    version = subprocess.check_output(
        ["/bin/bash", "-c", 'printf "%s.%s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"'],
        encoding="utf-8", timeout=10,
    )
    if version != "3.2":
        raise pytest.UsageError(f"Bash 3.2 lane found /bin/bash version {version!r}")
