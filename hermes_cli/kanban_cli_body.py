"""``hermes kanban create`` body input: ``--body`` or ``--body-file`` (``-`` = stdin). [daveey]"""
from __future__ import annotations

import sys
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
