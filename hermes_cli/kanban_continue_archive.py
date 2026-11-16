"""Verify explicitly published hub workspace archives before retaining pool placement."""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path


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
    if pin and pin["op"] == "pin" and portable(conn, task_id, checkpoint, trees=pin["trees"]):
        return None
    return pin


def portable(conn, task_id: str, checkpoint: str, *, trees=()) -> bool:
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
            declared = re.search(r"(?:^|\n)portable-workspace:\s*$", text[:match.start()], re.IGNORECASE)
            if not declared or task_id not in parts or '..' in parts:
                return False
            row = conn.execute("SELECT workspace_path FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return bool(row and row[0] and verify(path, match['sha'].lower(), row[0], trees))
    return False


def _probe(argv):
    from tools.environments.local import build_subprocess_env

    result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=20,
                            check=False, env=build_subprocess_env())
    value = result.stdout.strip()
    return value if result.returncode == 0 and re.fullmatch(r"[a-f0-9]{64}", value) else None


def verify(path: str, digest: str, workspace: str, trees=()) -> bool:
    """Compare actual hub archive bytes with the complete current local workspace.

    All pin-planner trees must be within the archived root. Local inventory both
    before and after the remote read fences edits made while the hub was hashing.
    Each probe has a hard deadline; any uncertainty retains the machine pin.
    """
    try:
        root = Path(workspace).resolve(strict=True)
        if any(not Path(tree).resolve(strict=True).is_relative_to(root) for tree in trees):
            return False
        script = Path(__file__).with_name("kanban_archive_inventory.py")
        local = [sys.executable, str(script), "tree", str(root)]
        before = _probe(local)
        if not before:
            return False
        remote = shlex.join(["python3", "-c", script.read_text(encoding="utf-8-sig"), "archive", path, digest])
        actual = _probe(["hub-admin", remote])
        return actual == before and _probe(local) == before
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
