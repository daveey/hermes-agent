"""Live-DB test-isolation guard and the per-process "last init error" record.
Every SessionDB construction resolves its path through _ensure_test_isolation
so a pytest-context process (env OR ancestry) can never open a production
state.db; env-based so subprocess children are protected too."""

import os
import sys
import threading
import weakref
from pathlib import Path
from typing import Any, Optional

try:  # Hard dependency, but tolerate scaffold-phase imports before pip install.
    import psutil
except ImportError:  # pragma: no cover - stripped/scaffold installs only
    psutil = None  # type: ignore[assignment]

# Field evidence: pytest fixture rows landed in the production state.db and a
# pytest-spawned child flipped the journal mode under the live WAL writer.

#: Env twin of ``_STATE_DB_GUARD_BYPASS`` for child processes (a module global
#: cannot cross a process boundary, and ancestry arms the guard there).
_STATE_DB_GUARD_BYPASS_ENV = "HERMES_STATE_DB_GUARD_BYPASS"


def _real_platform_state_root() -> Optional[Path]:
    """The REAL platform-default Hermes root. Avoids ``Path.home()`` /
    ``hermes_constants`` (tests monkeypatch Path.home to a tempdir); ``expanduser``
    reads HOME/passwd, which the conftest never rewrites."""
    try:
        home = Path(os.path.expanduser("~"))
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA", "").strip()
            root = Path(base) / "hermes" if base else home / "AppData" / "Local" / "hermes"
        else:
            root = home / ".hermes"
        return root.resolve()
    except Exception:
        return None


#: Exported by the hermetic conftest alongside the HERMES_HOME redirect. Unlike
#: PYTEST_* it is OURS and inherits by default, so a child carrying it that
#: resolves a production DB is by definition an isolation escape.
# : Env marker exported by the hermetic test conftest at the same moment it : redirects ``HERMES_HOME`` to
# the per-session tmp isolation root. Unlike ``PYTEST_*`` (owned by pytest, and : routinely scrubbed by
# tests that rebuild a child environment), this marker : is OURS: it declares "this process tree is running
# under Hermes test : isolation", and it inherits into subprocess children by default — so a : child that
# received the patched ``HERMES_HOME`` also received the marker, : and a child that resolves a production DB
# while carrying it is, by : definition, an isolation escape (#82770).
_TEST_ISOLATION_MARKER_ENV = "HERMES_TEST_ISOLATION"


def _running_under_pytest() -> bool:
    """True when this process (or a parent test process) is a pytest run."""
    return bool(
        os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("PYTEST_VERSION")
        or os.environ.get(_TEST_ISOLATION_MARKER_ENV)
    )


#: pytest launcher names, matched against each argv token's *basename* so
#: ``/tmp/pytest-of-dev/...`` paths cannot false-positive.
_PYTEST_LAUNCHER_NAMES = frozenset({"pytest", "py.test", "pytest.exe", "py.test.exe"})
#: The repo's test runner: under its fork server (HERMES_TEST_FORKSERVER) a test
#: process has no ``python -m pytest`` ancestor, only ``python .../run_tests_parallel.py``.
#: It counts only as the script a Python interpreter runs: a prompt or an editor
#: naming the file (``hermes chat -q "fix scripts/run_tests_parallel.py"``) is no test.
_TEST_RUNNER_SCRIPT = "run_tests_parallel.py"

#: Memoised ancestry answer: the tree above us doesn't change; keep the hot path free.
_PYTEST_ANCESTOR: Optional[bool] = None


def _process_looks_like_pytest(proc: Any) -> bool:
    """True when *proc*'s command line is a pytest invocation. Unreadable cmdline
    => not pytest: guessing the other way would refuse production opens."""
    try:
        cmdline = proc.cmdline() or []
    except Exception:
        return False
    names = []
    for arg in cmdline:
        try:
            # Split on both separators on every host so the answer is platform-independent.
            names.append(str(arg).strip('"').strip("'").replace("\\", "/").rsplit("/", 1)[-1].lower())
        except Exception:
            names.append("")
    if any(name in _PYTEST_LAUNCHER_NAMES for name in names):
        return True
    if not names or not names[0].startswith("python"):
        return False
    script = next((name for name in names[1:] if not name.startswith("-")), "")
    return script == _TEST_RUNNER_SCRIPT


def _has_pytest_ancestor() -> bool:
    """True when an ancestor process is a pytest run: a child spawned with a
    rebuilt env loses PYTEST_* and the HERMES_HOME redirect together, ancestry
    survives that. Fails open without psutil / on walk errors.

    ``_running_under_pytest`` reads ``PYTEST_*`` env vars, which a child spawned with a rebuilt environment
    loses at the same moment it loses the ``HERMES_HOME`` redirect: that child aims at the production DB
    *and* disarms the guard in one step (#82770). Ancestry is the one test-context signal that survives an
    env rebuild, so it backs the env check up.
    """
    global _PYTEST_ANCESTOR
    if _PYTEST_ANCESTOR is not None:
        return _PYTEST_ANCESTOR
    found = False
    if psutil is not None:
        try:
            found = any(_process_looks_like_pytest(p) for p in psutil.Process().parents())
        except Exception:
            found = False
    _PYTEST_ANCESTOR = found
    return found


def _in_test_context() -> bool:
    """Test run by environment or ancestry (memoised; env checked first)."""
    return _running_under_pytest() or _has_pytest_ancestor()


def _is_production_state_db(resolved: Path, root: Path) -> bool:
    """*resolved* is ``<root>/state.db`` or ``<root>/profiles/<name>/state.db``;
    deeper scratch paths (repo worktrees) are deliberately NOT matched."""
    if resolved.parent == root:
        return True
    try:
        parts = resolved.relative_to(root).parts
    except ValueError:
        return False
    return len(parts) == 3 and parts[0] == "profiles"


def _account_hermes_root() -> Optional[Path]:
    """The OS account's own Hermes root, from the password database on POSIX: tests rebind ``HOME`` to a
    tempdir, which would move ``_real_platform_state_root`` onto their own boards. [daveey]"""
    if sys.platform == "win32":
        return _real_platform_state_root()
    try:
        import pwd

        return (Path(pwd.getpwuid(os.getuid()).pw_dir) / ".hermes").resolve()
    except (ImportError, KeyError, OSError):
        return _real_platform_state_root()


def _is_production_kanban_db(resolved: Path, root: Path) -> bool:
    """*resolved* is the shared board ``<root>/kanban.db`` or a named board ``<root>/kanban/boards/<slug>/kanban.db``."""
    try:
        parts = resolved.relative_to(root).parts
    except ValueError:
        return False
    return parts == ("kanban.db",) or (len(parts) == 4 and parts[:2] == ("kanban", "boards") and parts[3] == "kanban.db")


def ensure_kanban_test_isolation(db_path: Path) -> None:
    """Raise before any mkdir/open when a pytest-context process (env OR ancestry) resolves a production
    Kanban board. The tests/conftest.py guard covers only files under ``tests/``: a pytest file elsewhere
    (an agent's scratch probe) or ``--noconftest`` inherits a worker's ``HERMES_KANBAN_DB`` pin and wrote
    live cards (t_f6b0a5ea, t_13317706, t_4d53a0f1). This choke point needs no conftest. [daveey]"""
    if not _in_test_context():
        return
    root = _account_hermes_root()
    if root is None:
        return
    resolved = Path(db_path).expanduser().resolve()
    if _is_production_kanban_db(resolved, root):
        raise RuntimeError(
            f"live-system guard: a test resolved the production kanban board {resolved} (under the account's "
            f"Hermes root {root}). Isolate HERMES_KANBAN_HOME and clear HERMES_KANBAN_DB/HERMES_KANBAN_BOARD "
            "in the fixture, and run tests from the repo's tests/ tree so its conftest sandbox applies."
        )


def ensure_origin_work_test_isolation(path: Path) -> None:
    """Raise before a pytest-context process (env OR ancestry) writes the account's live ``<root>/origin-work.db``.
    Plugin suites with their own conftest swap HERMES_KANBAN_HOME per test; background saves landed ~20 MB of
    test-scope rows in the hub's live file (t_01a1dadf165f8c53). Needs no conftest, like the board guard. [daveey]"""
    if not _in_test_context():
        return
    root = _account_hermes_root()
    if root is not None and Path(path).expanduser().resolve() == root / "origin-work.db":
        raise RuntimeError(
            f"live-system guard: a test resolved the production origin-work snapshot {path} (under the account's "
            f"Hermes root {root}). Isolate HERMES_KANBAN_HOME in the fixture."
        )


# Test-only SessionDB instance registry. Under the hermetic suite every
# successfully constructed SessionDB is added to this WeakSet so the autouse
# teardown in tests/conftest.py (_close_leaked_session_dbs) can close whatever
# a test forgot to close. Dozens of tests build SessionDB() directly and never
# close it; each instance holds a writer connection plus pooled readers, and a
# single-process run over tests/hermes_cli/ accumulated 16-25 GB RSS (OOM
# incident 20260816). The per-file runner masks this in CI; the registry fixes
# the class at the source instead of patching ~40 test files.
#
# Population is gated on the isolation marker (exported by tests/conftest.py
# before any test module imports). Production processes never populate it;
# do not "simplify" the gate away. WeakSet membership never pins an instance.
_test_instance_registry: weakref.WeakSet[Any] = weakref.WeakSet()


def _register_test_instance(db: Any) -> None:
    """Track *db* for suite-level teardown closing (test-isolation runs only)."""
    if os.environ.get(_TEST_ISOLATION_MARKER_ENV):
        try:
            _test_instance_registry.add(db)
        except Exception:  # pragma: no cover — registry must never break init
            pass


# Last SessionDB() init error, per-process; surfaced by /resume-style slash
# commands so users know WHY. Only SessionDB.__init__ writes it.
_last_init_error: Optional[str] = None
_last_init_error_lock = threading.Lock()


def _set_last_init_error(msg: Optional[str]) -> None:
    """Record (or clear with None) the most recent init failure. __init__ never
    clears on success: a concurrent open would erase the cause another thread's
    /resume is about to format."""
    global _last_init_error
    with _last_init_error_lock:
        _last_init_error = msg


def get_last_init_error() -> Optional[str]:
    """Most recent state.db init failure (None if none/never attempted)."""
    return _last_init_error
