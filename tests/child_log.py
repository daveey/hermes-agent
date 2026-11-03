"""A log file a child process writes while the test reads it.

    with ChildLog(tmp_path / 'gateway.log') as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        ...
        assert ready, log.text()

Don't hand a child a ``'w+'`` handle and poll it with ``log.seek(0); log.read()``. The child
inherits that open file description, so it shares the parent's file offset. Each ``seek(0)`` moves
the child's next write to offset 0, where it overwrites what is already there: a poll throttled
between its seek and read misses a READY line, and a ``seek(0)`` left behind after a passing
readiness check makes everything the child writes later overwrite the start of the log. A
ChildLog is an ``O_APPEND`` handle (every write lands at the end, whatever the offset), and
``text()`` reads the file by path, so the test never moves the child's offset.
"""

from __future__ import annotations

import io
import os
from pathlib import Path


def _truncating_opener(path: str, flags: int) -> int:
    # FileIO's 'a' is O_WRONLY|O_CREAT|O_APPEND; truncate as well, like the 'w+' this replaces.
    return os.open(path, flags | os.O_TRUNC, 0o644)


class ChildLog(io.FileIO):
    """An append-only file to pass as a child's stdout/stderr, read back by path with ``text()``."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        super().__init__(self.path, 'a', opener=_truncating_opener)

    def text(self) -> str:
        """The whole log so far, read by path (never through the child's shared offset)."""
        try:
            return self.path.read_text(encoding='utf-8', errors='replace')
        except FileNotFoundError:
            return ''
