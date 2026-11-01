"""The repository suite requires pytest's collection-time safety fixtures."""
import os
import sys
from pathlib import Path


def _running_under_unittest_cli() -> bool:
    """True for ``python -m unittest ...``; pytest and plain importers (evals, e2e seeds) are not."""
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    return getattr(spec, "name", "") in {"unittest", "unittest.__main__"}


def _live_hermes_home():
    """The live Hermes home this process would write to, or None for an isolated one.

    Same rule as tests/conftest.py ``_hermes_home_points_at_production``: unset, the real
    root, a profile under it, or anything nested in it (a Hermes shell's ``mktemp -d``
    lands in ``<root>/cache/scratch``) is live.
    """
    try:
        from hermes_state_guard import _real_platform_state_root

        root = _real_platform_state_root() or (Path.home() / ".hermes").resolve()
    except Exception:
        root = (Path.home() / ".hermes").resolve()
    value = os.environ.get("HERMES_HOME", "").strip()
    if not value:
        return root
    try:
        resolved = Path(value).expanduser().resolve()
    except Exception:
        return root
    return resolved if resolved.is_relative_to(root) else None


# unittest never loads conftest.py, so a test module imported here would log its fixtures'
# deliberate failures (fake EACCES, cannot fork) into the live home's errors.log, where the
# coach self-heal lane files them as real defects (agent-dada3769d1), or write the live
# config (the 11-01 hub config wipe). Refuse before any child module is imported. Named
# modules, discovery below tests/ and repository discovery all import this package first.
if _running_under_unittest_cli():
    _live = _live_hermes_home()
    if _live is not None:
        raise SystemExit(
            f"Refusing to run Hermes tests under unittest against the live Hermes home {_live}:\n"
            "unittest skips tests/conftest.py, so test fixtures would write its logs and stores.\n"
            "Use scripts/run_tests.sh <test paths>, or give unittest a throwaway home:\n"
            "  HERMES_HOME=$(mktemp -d -p /tmp) python -m unittest <module names>"
        )


def load_tests(loader, tests, pattern):
    """Stop unittest discovery before it imports children without conftest isolation.

    Explicit, self-contained unittest modules remain runnable. Repository discovery
    bypasses credential stripping and live-state write fences as well as temporary
    Hermes homes.
    """
    raise RuntimeError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")
