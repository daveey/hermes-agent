"""cron.unreachable_retry imports into a scheduler still running the previous release.

The previous release's scheduler imports this module lazily (``from cron.unreachable_retry import
will_retry``) after an update has swapped the tree, while the cron.constants it already holds
predates ``is_recurring``. That ImportError lost the one-shot job's bookkeeping and the job ran
twice (release run 40717630046, e2e-upgrade handoff, test_cron_job_due_mid_update_runs_exactly_once).
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_OLD_PROCESS = """
import cron.constants as constants
del constants.is_recurring  # the previous release's cron.constants, already in memory
from cron.unreachable_retry import is_recurring, will_retry
assert is_recurring({"schedule": {"kind": "cron"}})
assert not is_recurring({"schedule": None})
assert not is_recurring({"schedule": {"kind": "once"}})
print("ok")
"""


def test_imports_under_an_older_in_memory_cron_constants():
    proc = subprocess.run([sys.executable, "-c", _OLD_PROCESS], cwd=ROOT, capture_output=True, text=True,
                          timeout=120, check=False)
    assert proc.returncode == 0 and proc.stdout.strip() == "ok", proc.stderr[-2000:]
