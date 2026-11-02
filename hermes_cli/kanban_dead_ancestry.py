"""Read-only diagnosis of ancestry probes pinned to rebased commits. [daveey]

Patch equivalence is evidence to replan, not evidence that a deployment is live.
Unknown shell syntax, missing objects and exhausted budgets remain ordinary waits.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from pathlib import Path

EVENT = "wake_check_dead_ancestry"
BUDGET_SECONDS = 10
_HEX = re.compile(r"[0-9a-fA-F]{7,40}\Z")


def ancestry_probes(cmd: str, cwd: str) -> list[tuple[str, str]]:
    """Recognize literal git commands only, never quoted script/echo contents."""
    try:
        lexer = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    # Do not guess shell-mutated cwd or environment. Those probes still run normally.
    if any(t in ("cd", "pushd", "eval", "export") or "$" in t or "`" in t for t in tokens):
        return []
    found = []
    for i, token in enumerate(tokens):
        if token != "git" or (i and tokens[i - 1] not in (";", "&&", "||", "|")):
            continue
        j, directory = i + 1, Path(cwd)
        while j + 1 < len(tokens) and tokens[j] == "-C":
            path = Path(os.path.expanduser(tokens[j + 1]))
            directory = path if path.is_absolute() else directory / path
            j += 2
        args = tokens[j:j + 4]
        if (len(args) == 4 and args[:2] == ["merge-base", "--is-ancestor"]
                and _HEX.fullmatch(args[2]) and args[3] not in (";", "&&", "||", "|")):
            found.append((str(directory.resolve()), args[2]))
    return found


def _git(repo: str, args: list[str], deadline: float, *, data: str | None = None) -> str:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired("git", BUDGET_SECONDS)
    result = subprocess.run(["git", "--no-pager", "-C", repo, *args],
                            input=data, capture_output=True, text=True,
                            timeout=remaining, check=True,
                            env={**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"})
    return result.stdout.strip()


def _patch_id(repo: str, sha: str, deadline: float) -> str:
    diff = _git(repo, ["show", "--format=", "--no-ext-diff", "--no-textconv", sha, "--"], deadline)
    output = _git(repo, ["patch-id", "--stable"], deadline, data=diff)
    return output.split()[0] if output else ""


def _landed(repo: str, sha: str, deadline: float) -> dict | None:
    full = _git(repo, ["rev-parse", "--verify", f"{sha}^{{commit}}"], deadline)
    try:
        upstream = _git(repo, ["symbolic-ref", "refs/remotes/origin/HEAD"], deadline)
    except subprocess.CalledProcessError:
        upstream = "refs/remotes/origin/main"
    tip = _git(repo, ["rev-parse", "--verify", f"{upstream}^{{commit}}"], deadline)
    try:
        _git(repo, ["merge-base", "--is-ancestor", full, tip], deadline)
        return None
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 1:
            raise
    cherry = _git(repo, ["cherry", tip, full, f"{full}~1"], deadline)
    if f"- {full}" not in cherry.splitlines():
        return None
    patch_id = _patch_id(repo, full, deadline)
    if not patch_id:
        return None
    landed = _find_patch(repo, tip, full, patch_id, deadline)
    if landed:
        return {"repo": repo, "sha": full, "landed": landed, "upstream": upstream}
    return None


def _find_patch(repo: str, tip: str, full: str, patch_id: str, deadline: float) -> str | None:
    # Stream diffs: no per-commit process loop and no full-history diff in Python memory.
    env = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}
    log = subprocess.Popen(
        ["git", "--no-pager", "-C", repo, "log", "--no-merges", "--no-ext-diff",
         "--no-textconv", "--no-renames", "-p", tip, "--not", f"{full}~1", "--"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        ids = subprocess.run(["git", "-C", repo, "patch-id", "--stable"], stdin=log.stdout,
                             capture_output=True, text=True, timeout=remaining, check=True, env=env)
        if log.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
            return None
        for line in ids.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == patch_id:
                return parts[1]
        return None
    finally:
        if log.poll() is None:
            log.kill()
        log.wait(timeout=1)
        if log.stdout:
            log.stdout.close()


def detect(cmd: str, cwd: str) -> list[dict]:
    """Return proven equivalents within a single bounded local-only budget; never fetch."""
    deadline = time.monotonic() + BUDGET_SECONDS
    hits = []
    for repo, sha in ancestry_probes(cmd, cwd):
        try:
            hit = _landed(repo, sha, deadline)
        except (OSError, subprocess.SubprocessError, UnicodeError):
            continue
        if hit and hit not in hits:
            hits.append(hit)
    return hits


def wake_once(conn, task_id: str, cmd: str, hits: list[dict]) -> str | None:
    """Apply within the caller's generation-checked transaction, once per repo/SHA."""
    import json

    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_wake_check import _wake

    seen = set()
    for (raw,) in conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                              (task_id, EVENT)):
        payload = json.loads(raw or "{}")
        seen.update((h["repo"], h["sha"]) for h in payload.get("premises", []))
    fresh = [h for h in hits if (h["repo"], h["sha"]) not in seen]
    if not fresh:
        return None
    note = "\n".join(
        f"wake_check waits for {h['sha']}, which landed on {h['upstream']} as {h['landed']}; "
        "the original identity is not upstream. "
        f"Re-park on {h['landed']} or run the check now."
        for h in fresh)
    woke = _wake(conn, task_id, EVENT, {"wake_check": cmd, "premises": fresh, "note": note})
    if woke:
        kb.add_comment(conn, task_id, "kanban-dispatcher", note)
    return woke
