"""``hermes kanban create`` body input: ``--body`` or ``--body-file`` (``-`` = stdin). [daveey]"""
from __future__ import annotations

import sys
import os
from pathlib import Path
from typing import Optional


def read_body(args) -> tuple[Optional[str], Optional[str]]:
    """``(body, error)``: the two flags are mutually exclusive; a file read failure is an error."""
    body = args.body
    body_file = getattr(args, "body_file", None)
    if body is not None and body_file is not None:
        return None, "kanban: --body and --body-file are mutually exclusive"
    if body_file is None:
        return body, None
    try:
        return (sys.stdin.read() if body_file == "-" else Path(body_file).read_text(encoding="utf-8-sig")), None
    except OSError as exc:
        return None, f"kanban: --body-file: {exc}"


def session_identity(in_worker):
    """Workers inherit their creator later; interactive CLI uses persisted chat IDs."""
    session_id = None
    session_key = "" if in_worker else os.environ.get("HERMES_SESSION_KEY", "").strip()
    if not in_worker:
        from tools.kanban_tools import _persisted_session_id
        session_id = (_persisted_session_id(session_key)
                      or _persisted_session_id(os.environ.get("HERMES_SESSION_ID")))
    return session_key, session_id
