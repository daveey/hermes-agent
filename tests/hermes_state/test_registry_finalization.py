"""A shared SessionDB finalizer must not import during interpreter shutdown.

The derived-session CI hang ended in __del__ -> close -> _get_module_lock.
Hold that real import lock in an owned daemon to make the shutdown boundary
repeatable, without relying on the CI thread's unidentified scheduling.
"""

import builtins
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


_EXIT_PROBE = """
import _imp
import faulthandler
import os
import sys
import threading
from pathlib import Path
import hermes_state
import hermes_state_registry as registry

original_del = hermes_state.SessionDB.__del__
def observe_del(self, original=original_del, write=os.write, finalizing=sys.is_finalizing):
    if finalizing():
        write(2, b'finalizing-db-enter\\n')
    original(self)
    write(2, b'finalizing-db-return\\n')
hermes_state.SessionDB.__del__ = observe_del

db = registry.acquire(Path(sys.argv[1]) / 'state.db')
del db  # Only the registry owns it: clearing that module triggers __del__.
faulthandler.dump_traceback_later(15, exit=True)
# A __main__ target retains its globals (including registry) and can suppress
# the finalizer entirely. Give the daemon its own minimal global namespace.
namespace = {}
exec('import _imp, threading\\ndef park(ready, lock):\\n'
     '    if lock: _imp.acquire_lock()\\n'
     '    ready.set()\\n'
     '    threading.Event().wait()\\n', namespace)
ready = threading.Event()
threading.Thread(target=namespace['park'], args=(ready, sys.argv[2] == 'locked'), daemon=True).start()
assert ready.wait(10), 'daemon never reached the controlled boundary'
del namespace
print('main-returning', flush=True)
"""


@pytest.mark.parametrize("lock_state", ["unlocked", "locked"])
def test_shared_db_finalizer_exits_without_import_lock(tmp_path, lock_state):
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_EXIT_PROBE), str(tmp_path), lock_state],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, encoding="utf-8", timeout=45, check=False,
    )
    assert "main-returning" in result.stdout, result.stderr
    assert "finalizing-db-enter" in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
    assert "finalizing-db-return" in result.stderr, result.stderr


def test_close_releases_shared_refs_without_importing_registry(tmp_path, monkeypatch):
    import hermes_state_registry as registry

    first = registry.acquire(tmp_path / "state.db")
    second = registry.acquire(tmp_path / "state.db")
    assert first is second
    original_import = builtins.__import__

    def refuse_registry_import(name, *args, **kwargs):
        assert name != "hermes_state_registry", "close must use its already-bound owner"
        return original_import(name, *args, **kwargs)

    try:
        with monkeypatch.context() as patcher:
            patcher.delitem(sys.modules, "hermes_state_registry")
            patcher.setattr(builtins, "__import__", refuse_registry_import)
            first.close()
            assert second._conn is not None, "first release must preserve the other holder"
            second.close()
            assert second._conn is None, "final release must physically close"
    finally:
        registry.close_all()
