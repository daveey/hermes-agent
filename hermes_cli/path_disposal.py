"""One-shot host-owned path disposal, shared by plugin callers and custody policy."""

from __future__ import annotations

import errno
import logging
import os
import shutil
import stat
from pathlib import Path

from agent.file_safety import is_write_denied
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


def dispose_path(path: str, *, actor: str, reason: str, empty_only: bool = False) -> dict:
    """Dispose of one scoped path without following its final symlink.

    Return JSON data with path, outcome (disposed/missing/refused/error), and detail.
    A refusal is final: callers must not fall back to their own deletion.
    Check and removal deliberately live together here so custody policy can veto
    disposal before any filesystem mutation.
    """
    if not isinstance(path, str):
        raise TypeError("path must be a string")
    if not isinstance(reason, str):
        raise TypeError("reason must be a string")
    if not reason.strip():
        raise ValueError("reason must not be empty or blank")
    if not isinstance(actor, str):
        raise TypeError("actor must be a string")
    if not isinstance(empty_only, bool):
        raise TypeError("empty_only must be a bool")
    if not path or "\x00" in path:
        raise ValueError("path must not be empty or contain NUL")

    checked = os.path.abspath(path)

    def result(outcome: str, detail: str = "") -> dict:
        logger.info("Path disposal actor=%r reason=%r path=%r outcome=%s detail=%r empty_only=%s",
                    actor, reason, checked, outcome, detail, empty_only)
        return {"path": checked, "outcome": outcome, "detail": detail}

    try:
        candidate = Path(path)
        # Resolve parents before normalizing '..'; abspath first would hide a
        # symlink-parent traversal. Never resolve the final link itself.
        checked = os.path.abspath(candidate.parent.resolve() / candidate.name)
        target = Path(checked)
        home = get_hermes_home().resolve()
        tmp = Path("/tmp").resolve()  # no-tmp: ok — POSIX alias detection, not scratch allocation
        in_home = target != home and target.is_relative_to(home)
        tmp_parts = target.relative_to(tmp).parts if target.is_relative_to(tmp) else ()
        in_tmp = len(tmp_parts) > 1 and tmp_parts[0].startswith("hermes-")
        if target == home or not (in_home or in_tmp):
            return result("refused", "path is not strictly inside an allowed disposal root")
        if is_write_denied(checked):
            return result("refused", "path is write-denied")
        try:
            mode = target.lstat().st_mode
        except FileNotFoundError as exc:
            return result("missing", str(exc))
        if empty_only:
            if not stat.S_ISDIR(mode):
                return result("refused", "empty_only: path is not a directory")
            try:
                os.rmdir(target)
            except OSError as exc:
                if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    return result("refused", "empty_only: directory is not empty")
                raise
        elif stat.S_ISDIR(mode):
            shutil.rmtree(target)
        else:
            target.unlink()
        return result("disposed")
    except OSError as exc:
        return result("error", str(exc))
