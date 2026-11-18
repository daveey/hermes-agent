"""Version gate for execution premises that assume modern /bin/bash grammar.

Do not substitute Homebrew Bash: minor versions differ on arithmetic indirection.
Scanner-only tests and explicitly version-keyed Bash 3 controls need no gate.
"""
from functools import lru_cache
import subprocess

import pytest


@lru_cache(maxsize=1)
def system_bash_major() -> int:
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", 'printf "%s" "${BASH_VERSINFO[0]}"'],
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=5,
    )
    return int(result.stdout)


def require_modern_system_bash() -> str:
    """Gate only a real-shell premise, never a scanner fixture or whole module."""
    if system_bash_major() < 4:
        pytest.skip("execution premise requires /bin/bash >= 4; system Bash 3 has different grammar")
    return "/bin/bash"
