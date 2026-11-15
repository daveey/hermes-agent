"""Pure review policy for already repository-scoped Hermes changes.

This does not authorize shipping: automated still requires CI and a diff check.
Callers must supply complete, trusted Git patches, not truncated API previews.
"""


import io
import re
import tokenize
from collections.abc import Sequence
from pathlib import PurePosixPath


# Apply before the test allowlist, including to tests of these subsystems.
_SENSITIVE = re.compile(
    r"dispatch|schedul|selfwork|admission|kanban|credential|auth|secret|password|token|"
    r"api.?key|quota|pac(?:ing|er)|budget|bill|money|cost|pricing|payment|spend|"
    r"rate.?limit|account|usage|backoff|throttl|capacity|reservation",
    re.IGNORECASE,
)
_SHARED = re.compile(
    r"conftest|fixture|helper|support|harness|runner|run_tests|(?:^|[/_.-])ci(?:[/_.-]|$)|"
    r"workflow|build|config|setup|bootstrap|plugin|pytest|tox|nox|makefile|"
    r"docker|lockfile|package|requirements|pyproject",
    re.IGNORECASE,
)
_HUNK = re.compile(r"@@ -([0-9]{1,9})(?:,([0-9]{1,9}))? \+([0-9]{1,9})(?:,([0-9]{1,9}))? @@(?: .*)?")


def _valid_path(path):
    return (
        isinstance(path, str) and bool(path) and not path.startswith("/")
        and all(ord(char) >= 32 and ord(char) != 127 for char in path)
        and "\\" not in path and all(part not in {"", ".", ".."} for part in path.split("/"))
    )


def _patch_hunks(patch, path):
    """Validate a single-file unified patch; reject unfamiliar Git metadata."""
    if not isinstance(patch, str) or not patch or "\x00" in patch or not patch.endswith("\n"):
        return None
    lines = patch.splitlines(keepends=True)
    index = 0
    # Only ordinary regular-file modes are understood. No executable/symlink modes.
    prefixes = (
        f"diff --git a/{path} b/{path}\n",
        "new file mode 100644\n", "deleted file mode 100644\n",
    )
    while index < len(lines):
        line = lines[index]
        if line in prefixes or re.fullmatch(r"index [0-9a-f]+\.\.[0-9a-f]+(?: 100644)?\n", line):
            index += 1
        else:
            break
    if index < len(lines) and lines[index].startswith("--- "):
        if lines[index].rstrip("\n") not in {f"--- a/{path}", "--- /dev/null"}:
            return None
        index += 1
        if index >= len(lines) or lines[index].rstrip("\n") not in {f"+++ b/{path}", "+++ /dev/null"}:
            return None
        index += 1
    hunks = []
    old_end = new_end = 0
    changed = False
    while index < len(lines):
        match = _HUNK.fullmatch(lines[index].rstrip("\n"))
        if not match:
            return None
        old_start, old_count, new_start, new_count = (
            int(value) if value is not None else 1 for value in match.groups()
        )
        if (old_start < old_end or new_start < new_end
                or (old_start == 0 and old_count != 0) or (new_start == 0 and new_count != 0)):
            return None
        index += 1
        old, new, edits = [], [], []
        while index < len(lines) and not lines[index].startswith("@@ "):
            line = lines[index]
            if line == "\\ No newline at end of file\n":
                # Conservatively refuse rather than guess which side it modifies.
                return None
            if not line or line[0] not in " +-":
                return None
            prefix, text = line[0], line[1:]
            if prefix in " -":
                old.append(text)
            if prefix in " +":
                new.append(text)
            if prefix != " ":
                edits.append((prefix, text))
                changed = True
            index += 1
        if len(old) != old_count or len(new) != new_count:
            return None
        old_end, new_end = old_start + old_count, new_start + new_count
        hunks.append((old_start, new_start, old, new, edits))
    return hunks if hunks and changed else None


def _comment_tokens(source):
    """Tokenize a known file prefix, never an arbitrary hunk interior."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    if any(token.type == tokenize.ERRORTOKEN for token in tokens):
        return None
    return tokens


def _guard_comments_only(hunks):
    # Starting at line one establishes lexical state: '# prose' inside an unseen
    # triple-quoted string is NOT a comment. Gaps destroy that proof.
    old, new, edits = [], [], []
    for old_start, new_start, old_lines, new_lines, changed in hunks:
        if old_start != len(old) + 1 or new_start != len(new) + 1:
            return False
        old.extend(old_lines)
        new.extend(new_lines)
        edits.extend(changed)
    for _, text in edits:
        if not re.fullmatch(r"[ \t]*# [A-Za-z0-9 .,()'/-]+\n", text):
            return False
        # Tool directives are executable configuration, not wording. Keep this
        # allowlist intentionally narrow; unfamiliar punctuation stays full.
        if re.search(r"coding|noqa|type|fmt|health|pragma|doctest|ruff|pylint|mypy|pyright|isort|nosec|bandit|coverage|yapf|black", text, re.IGNORECASE):
            return False
    old_tokens = _comment_tokens("".join(old))
    new_tokens = _comment_tokens("".join(new))
    if old_tokens is None or new_tokens is None:
        return False
    # The edits must actually be comment tokens, not strings or continuations.
    for lines, tokens, prefix in ((old, old_tokens, "-"), (new, new_tokens, "+")):
        comment_lines = {token.start[0] for token in tokens if token.type == tokenize.COMMENT}
        edited_text = {text for side, text in edits if side == prefix}
        if any(number not in comment_lines for number, text in enumerate(lines, 1) if text in edited_text):
            return False
    ignored = {tokenize.COMMENT, tokenize.NL}
    return ([(token.type, token.string) for token in old_tokens if token.type not in ignored]
            == [(token.type, token.string) for token in new_tokens if token.type not in ignored])


def _change_reason(change):
    if not isinstance(change, dict) or not {"old_path", "new_path", "status", "patch", "binary"} <= change.keys():
        return "missing or malformed change metadata", False
    if change["binary"] is not False:
        return "binary or unknown file kind", False
    if change["status"] not in ("M", "A", "D"):
        return "unknown status or rename requires full review", False
    old, new = change["old_path"], change["new_path"]
    if change["status"] == "A" and old is None:
        old = new
    if change["status"] == "D" and new is None:
        new = old
    if not _valid_path(old) or not _valid_path(new):
        return "invalid repository-relative path", False
    if old != new:
        return "path change requires full review", False
    if _SENSITIVE.search(old):
        return "dispatcher, credentials, quota, pacing or money area", False
    if _SHARED.search(old):
        return "shared test infrastructure, CI, build or configuration area", False
    hunks = _patch_hunks(change["patch"], old)
    if hunks is None:
        return "missing, malformed or unsafe patch", False
    path = PurePosixPath(old)
    if old.startswith("tests/") and path.name.startswith("test_") and path.suffix == ".py":
        return "ordinary Python test-only change", True
    if (change["status"] == "M" and path.parts[0] in {"hermes_cli", "agent", "gateway", "tools", "cron"}
            and path.suffix == ".py" and re.search(r"(?:^|_)guards?(?:_|$)", path.stem)
            and _guard_comments_only(hunks)):
        return "lexically proven guard comment-only wording", True
    return "unrecognized production change requires full review", False


def classify_diff(changes):
    """Return a fail-closed tier without I/O, mutation or patch/secret disclosure.

    Empty input is not evidence of a safe diff. Renames are deliberately all full,
    even within a tier. Only trusted complete patches can establish file modes;
    callers must never omit Git mode metadata or substitute API patch previews.
    """
    if not isinstance(changes, Sequence) or isinstance(changes, (str, bytes)) or not changes:
        return {"risk": "full", "reasons": ["missing or malformed change sequence"]}
    results = [_change_reason(change) for change in changes]
    return {
        "risk": "automated" if all(allowed for _, allowed in results) else "full",
        "reasons": list(dict.fromkeys(reason for reason, _ in results)),
    }


def requires_full_check(changes):
    """Sampling differs from review: ordinary runtime fixes may be sampled.

    Sensitive paths and unknown/incomplete evidence always get a post-merge
    check. Reuse the same complete-patch validation rather than trust a title.
    """
    verdict = classify_diff(changes)
    sampleable = {
        "ordinary Python test-only change", "lexically proven guard comment-only wording",
        "unrecognized production change requires full review",
    }
    return any(reason not in sampleable for reason in verdict['reasons'])
