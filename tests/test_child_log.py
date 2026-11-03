"""ChildLog keeps a child's log intact while the test reads it (tests/child_log.py)."""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

import os
import subprocess
import sys

from tests.child_log import ChildLog
from tests.waiting import wait_until

_CHILD = "import sys\nprint('READY', flush=True)\nsys.stdin.readline()\nprint('NEXT', flush=True)\n"


def test_child_writes_land_at_eof_even_after_the_shared_offset_moves(tmp_path):
    path = tmp_path / 'child.log'
    path.write_text('stale output from an earlier run\n')
    with ChildLog(path) as log:
        assert log.text() == ''  # truncated, like the 'w+' it replaces
        child = subprocess.Popen([sys.executable, '-c', _CHILD], stdin=subprocess.PIPE,
                                 stdout=log, stderr=subprocess.STDOUT, text=True)
        try:
            assert child.stdout is None
            wait_until(lambda: 'READY' in log.text() or child.poll() is not None, timeout=30,
                       what='the child READY line')
            # The race: a poll's seek(0) on the description the child shares. Without O_APPEND the
            # child's next line lands at offset 0 and overwrites READY.
            os.lseek(log.fileno(), 0, os.SEEK_SET)
            child.communicate('go\n', timeout=30)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
        assert child.returncode == 0
        assert log.text() == 'READY\nNEXT\n'
