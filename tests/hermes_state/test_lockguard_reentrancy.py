"""Finalizers may release another handle while the guard is already held."""

import subprocess
import sys
import textwrap
import threading

import pytest

import hermes_state_lockguard as lg


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("operation", ["hold", "release"])
def test_session_finalizer_inside_guard_fd_scan(tmp_path, operation):
    # Isolate the intentional RED deadlock: no stuck thread or atexit cleanup may
    # retain the process-global guard in the pytest process.
    code = textwrap.dedent("""
        import gc
        import os
        import sys
        import threading
        import traceback
        import weakref
        from pathlib import Path
        import hermes_state_lockguard as lg
        import hermes_state_wal
        from hermes_state import SessionDB

        hermes_state_wal.is_sqlite_wal_reset_vulnerable = lambda version_info=None: False
        hermes_state_wal.resolve_journal_mode = lambda: 'wal'
        root, operation = Path(sys.argv[1]), sys.argv[2]
        errors = []

        def exercise():
            try:
                db = SessionDB(db_path=root / 'state.db')
                # hold: shared inode counts; release: both handles are last owners
                # of distinct inodes, so both enter their descriptor-unlock loops.
                other_path = db.db_path if operation == 'hold' else root / 'other.db'
                orphan = SessionDB(db_path=other_path)
                assert db._wal_lock_guard and orphan._wal_lock_guard
                orphan.cycle = orphan
                pending = [orphan]
                ref = weakref.ref(orphan)
                del orphan
                real_scan = lg._own_fds_for
                collected = []

                def scan(identities):
                    if pending:
                        pending.clear()
                        gc.collect()  # SessionDB.__del__ -> close -> release
                        assert ref() is None
                        collected.append(True)
                    yield from real_scan(identities)

                lg._own_fds_for = scan
                if operation == 'hold':
                    lg.hold(db.db_path, db._wal_lock_guard)
                    assert all(lg._HANDLES[i] == 1 for i in db._wal_lock_guard)
                    db.create_session('after-gc', 'cli')
                else:
                    db.close()
                lg._own_fds_for = real_scan
                assert collected == [True]
                db.close()
                assert not lg._HANDLES
            except BaseException:
                errors.append(traceback.format_exc())

        thread = threading.Thread(target=exercise, daemon=True)
        thread.start()
        thread.join(timeout=10)
        if thread.is_alive():
            print('finalizer thread still alive after join timeout', flush=True)
            os._exit(1)
        if errors:
            print(''.join(errors), flush=True)
            os._exit(2)
        print('finalizer completed; guard counts drained', flush=True)
    """)
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), operation],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "finalizer completed; guard counts drained" in result.stdout


def test_guard_still_excludes_other_threads():
    attempted = threading.Event()
    acquired = []

    def contender():
        attempted.set()
        entered = lg._LOCK.acquire(timeout=2)
        acquired.append(entered)
        if entered:
            lg._LOCK.release()

    with lg._LOCK:
        thread = threading.Thread(target=contender, daemon=True)
        thread.start()
        assert attempted.wait(timeout=10)
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert acquired == [False]
    # The same competing thread path succeeds once the owner leaves.
    thread = threading.Thread(target=contender, daemon=True)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert acquired == [False, True]
