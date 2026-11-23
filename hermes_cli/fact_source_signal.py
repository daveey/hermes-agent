"""Post-COMMIT native cue; WAL writes alone can precede visible commit. [daveey]"""
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def cue_path(path):
    return Path(str(path) + ".facts-commit")


def publish_commit(path):
    """Never turn an already-committed mutation into a retryable write failure.

    No content or cursor is carried here. The source's guarded revision read owns
    truth. Writer publication failure is logged; no polling fallback is allowed.
    """
    if not sys.platform.startswith("linux") or path is None or str(path) == ":memory:":
        return
    cue = cue_path(path)
    try:
        # O_NOFOLLOW prevents a cue symlink from modifying another file. Do not
        # create parent directories (a removed profile must remain removed).
        fd = os.open(cue, os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            os.utime(fd, None)
        finally:
            os.close(fd)
    except OSError:
        log.warning("post-commit fact cue unavailable", exc_info=True)
