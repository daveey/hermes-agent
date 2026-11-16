"""Verify explicitly published hub workspace archives before retaining pool placement."""
from __future__ import annotations

import re
import shlex
import subprocess


# Accept the existing checkpoint prose, but pair the path with its adjacent digest.
_ARCHIVE = re.compile(
    r"(?:hub-admin:|hermes-hub:|\bhub\s+(?:at\s+|on:\s*)?)"
    r"(?P<path>/[A-Za-z0-9_./-]+\.(?:tar\.gz|tgz|tar\.zst))"
    r"[`,\s]*sha256\s*[:=]?\s*(?P<sha>[a-fA-F0-9]{64})(?![a-fA-F0-9])",
    re.IGNORECASE,
)


def plan(conn, task_id: str, checkpoint: str):
    """Preserve existing pool constraints only with verified hub carriage."""
    from hermes_cli import kanban_park_pin

    pin = kanban_park_pin.plan(conn, task_id, checkpoint)
    if pin and pin["op"] == "pin" and portable(conn, task_id, checkpoint):
        return None
    return pin


def portable(conn, task_id: str, checkpoint: str) -> bool:
    """Only current checkpoint or newest post-move archive comment is eligible.

    Never fall back to an older archive after a newer reference fails verification.
    A task-id path component prevents unrelated artifacts granting portability.
    """
    texts = [checkpoint]
    # Comments must postdate both the last move and the current run's start: an earlier
    # run's archive does not carry that run's successor's unpushed work. No current run
    # (or same-second) means no comment is eligible.
    since = conn.execute(
        "SELECT MAX(COALESCE((SELECT MAX(created_at) FROM task_events "
        "WHERE task_id = :t AND kind = 'moved'), -1), COALESCE((SELECT r.started_at "
        "FROM tasks t JOIN task_runs r ON r.id = t.current_run_id WHERE t.id = :t), 1 << 62))",
        {"t": task_id},
    ).fetchone()[0]
    texts.extend(row[0] for row in conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? AND created_at > ? ORDER BY id DESC LIMIT 20",
        (task_id, since),
    ))
    for text in texts:
        match = _ARCHIVE.search(text or "")
        if match:
            path = match['path']
            parts = path.split('/')
            return task_id in parts and '..' not in parts and verify(path, match['sha'].lower())
    return False


def verify(path: str, digest: str) -> bool:
    """Hash on the hub, never on the worker with a coincidentally identical path."""
    from tools.environments.local import build_subprocess_env

    try:
        result = subprocess.run(
            ["hub-admin", f"sha256sum -- {shlex.quote(path)}"],
            capture_output=True, text=True, encoding="utf-8", timeout=20, check=False,
            env=build_subprocess_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == f"{digest}  {path}"
