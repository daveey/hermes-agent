"""Non-fatal filing advice for human asks with observable premises. [daveey]

Suggestions are never executed or installed: the worker must confirm the target,
resolution semantics and probe host before supplying resolved_check.
"""
import re
import shlex

_PR = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/([1-9][0-9]*)(?=$|[\s/#?.,)\]`])")
_PATH = re.compile(r"`(/[^`\n]+)`|\"(/[^\"\n]+)\"|'(/[^'\n]+)'|(?:^|[\s(])(/[^\s`\"'<>;|]+)")
_BUILD = re.compile(r"\b(?:build|version)\s+[vV]?[0-9][\w.-]*\b|\bios-app[0-9]+\b", re.IGNORECASE)
_VERSION_READ = re.compile(r"(?:version read|version command|version probe)\s*:\s*`([^`\n]+)`", re.IGNORECASE)


def warning_fields(request: dict | None) -> dict:
    """Use the lifecycle result's advisory-field convention; never reject an ask."""
    if not request or request.get("resolved_check"):
        return {}
    text = "\n".join(str(request.get(key) or "") for key in ("action", "evidence"))
    suggestions = []
    for match in _PR.finditer(text):
        repo, number = match.groups()
        suggestions.append(
            f"state=$(gh pr view {number} -R {shlex.quote(repo)} --json state -q .state) || exit 2; "
            'case "$state" in MERGED|CLOSED) exit 0;; OPEN) exit 1;; *) exit 2;; esac')
    for match in _PATH.finditer(text):
        quoted = next((part for part in match.groups()[:3] if part is not None), None)
        path = shlex.quote(quoted if quoted is not None else match.group(4).rstrip(".,):]"))
        suggestions.append(f"test -e {path} (or ! test -e {path} if absence resolves the ask)")
    if _BUILD.search(text):
        read = _VERSION_READ.search(text)
        source = f"`{read.group(1)}`" if read else "the existing version read used to establish this premise"
        suggestions.append(f"Reuse {source}, comparing its output with the required build/version; "
                           "do not invent a device endpoint or treat read failures as resolution")
    if not suggestions:
        return {}
    return {"resolved_check_warning": (
        "Machine-checkable premise without human_request.resolved_check: re-test before asking; "
        "attach a verified probe so a changed premise resumes the card automatically. "
        "Suggested probes (review target host and resolution condition; not executed): "
        + "; ".join(dict.fromkeys(suggestions))
        + ". Exit 0 only when the human step is no longer needed, 1 while pending, 2 on read failure. "
          "A closed PR or missing path may require replanning, not completion. This block still succeeded.")}
