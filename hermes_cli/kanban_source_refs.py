"""Inert local Git custody refs. No producer, no deletion of unrelated locks.

Callers record a pending commit unit before :func:`create_ref`. A timeout or
conflict leaves the ref and any Git lock in place. Unknown snapshot proof is
not a complete snapshot.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from typing import Callable

from hermes_cli.git_budget import GitUnchecked, timeout_for
from hermes_cli.kanban_source_barrier import read_regular

GIT_INPUT_MAX_BYTES = 16 * 1024 * 1024

CUSTODY_PREFIX = "refs/hermes-source-custody"
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REFLOG = re.compile(
    r"^((?:[0-9a-f]{40}|[0-9a-f]{64})) ((?:[0-9a-f]{40}|[0-9a-f]{64})) .+ <[^>]*> (\d+) [+-]\d{4}(?:\t.*)?\Z")
GitBudget = Callable[[float], float]


def custody_ref(store_uuid: str, op_id: str, unit_id: str, oid: str) -> str:
    """``refs/hermes-source-custody/<store>/<op>/<unit>/<oid>``."""
    if _HEX32.fullmatch(store_uuid) is None or _HEX32.fullmatch(op_id) is None:
        raise ValueError("store and operation ids must be 32 hex characters")
    if not unit_id or "/" in unit_id or len(unit_id) > 128:
        raise ValueError("unit id is not a single path segment")
    if _OID.fullmatch(oid) is None:
        raise ValueError("object id must be a full sha1 or sha256 hex")
    return f"{CUSTODY_PREFIX}/{store_uuid}/{op_id}/{unit_id}/{oid}"


def git_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Credential helpers stay. Grafts, replace refs and lazy fetch do not."""
    env = dict(os.environ if base is None else base)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_GRAFT_FILE"] = "/dev/null"
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        env.pop(key, None)
    return env


def run_git(repo: Path, args: list[str], *, budget: GitBudget | None = None, cap: float = 10.0,
            stdin_text: str | None = None) -> dict:
    """One literal argv. Timeout is unknown, and this function never unlinks a lock."""
    try:
        timeout = timeout_for(budget, min(cap, 1.0) if cap <= 1.0 else cap)
    except GitUnchecked as exc:
        return {"status": "unknown", "reason": str(exc), "rc": None, "stdout": "", "stderr": ""}
    if timeout <= 0:
        return {"status": "unknown", "reason": "budget", "rc": None, "stdout": "", "stderr": ""}
    try:
        if stdin_text is None:
            result = subprocess.run(
                ["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, env=git_env(), check=False)
        else:
            result = subprocess.run(
                ["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout, input=stdin_text, env=git_env(), check=False)
    except subprocess.TimeoutExpired:
        return {"status": "unknown", "reason": "timeout", "rc": None, "stdout": "", "stderr": ""}
    except OSError as exc:
        return {"status": "unknown", "reason": type(exc).__name__, "rc": None, "stdout": "", "stderr": ""}
    return {"status": "ok", "reason": None, "rc": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr}


def _git_path(repo: Path, name: str, *, budget: GitBudget | None) -> Path | None:
    found = run_git(repo, ["rev-parse", "--git-path", name], budget=budget)
    if found["status"] != "ok" or found["rc"] != 0:
        return None
    path = Path(found["stdout"].strip())
    return path if path.is_absolute() else Path(repo) / path


def repo_identity(repo: Path, *, budget: GitBudget | None = None) -> dict:
    """Common-gitdir device/inode plus the lexical and canonical paths."""
    found = run_git(repo, ["rev-parse", "--git-common-dir"], budget=budget)
    if found["status"] != "ok" or found["rc"] != 0:
        return {"status": "unknown", "reason": "common_gitdir"}
    lexical = Path(found["stdout"].strip())
    if not lexical.is_absolute():
        lexical = Path(repo) / lexical
    try:
        canonical = lexical.resolve(strict=True)
        info = os.stat(canonical)
    except OSError:
        return {"status": "unknown", "reason": "common_gitdir"}
    return {"status": "ok", "dev": info.st_dev, "ino": info.st_ino,
            "lexical": str(lexical), "canonical": str(canonical)}


def _read_ref(repo: Path, ref: str, *, budget: GitBudget | None) -> str | None:
    found = run_git(repo, ["rev-parse", "--verify", "--end-of-options", ref], budget=budget)
    if found["status"] != "ok" or found["rc"] != 0:
        return None
    text = found["stdout"].strip()
    return text if _OID.fullmatch(text) else None


def create_ref(repo: Path, ref: str, oid: str, *, budget: GitBudget | None = None) -> dict:
    """Create-only. Format mismatch is invalid. A timeout leaves locks untouched."""
    if not ref.startswith(CUSTODY_PREFIX + "/") or _OID.fullmatch(oid) is None:
        return {"status": "invalid", "ref": ref, "oid": oid}
    fmt_result = run_git(repo, ["rev-parse", "--show-object-format"], budget=budget)
    if fmt_result["status"] != "ok" or fmt_result["rc"] != 0:
        return {"status": "unknown", "reason": fmt_result["reason"] or "object_format", "ref": ref, "oid": oid}
    fmt = fmt_result["stdout"].strip()
    expected = 40 if fmt == "sha1" else 64 if fmt == "sha256" else None
    if expected is None or len(oid) != expected:
        return {"status": "invalid", "reason": "object_format", "ref": ref, "oid": oid}
    identity = repo_identity(repo, budget=budget)
    symbol = run_git(repo, ["symbolic-ref", "-q", ref], budget=budget)
    if symbol["status"] != "ok" or symbol["rc"] != 1:
        return {"status": "unknown", "reason": "symbolic_or_unreadable", "ref": ref, "oid": oid}
    result = run_git(repo, ["update-ref", "--no-deref", ref, oid, "0" * len(oid)], budget=budget, cap=1.0)
    if result["status"] != "ok":
        reconciled = _read_ref(repo, ref, budget=budget)
        if reconciled == oid:
            return {"status": "reconciled", "ref": ref, "oid": oid, "identity": identity}
        return {"status": "unknown", "reason": result["reason"], "ref": ref, "oid": oid, "identity": identity}
    if result["rc"] == 0:
        return {"status": "created", "ref": ref, "oid": oid, "identity": identity}
    current = _read_ref(repo, ref, budget=budget)
    stderr = result["stderr"] or ""
    if "cannot lock" in stderr and "already exists" not in stderr:
        return {"status": "unknown", "reason": "lock", "ref": ref, "oid": oid, "identity": identity}
    if "nonexistent object" in stderr:
        return {"status": "invalid", "reason": "object", "ref": ref, "oid": oid}
    if current == oid:
        return {"status": "conflict", "reason": "exists", "ref": ref, "oid": oid, "identity": identity}
    if current is None:
        return {"status": "unknown", "reason": "unreconciled", "ref": ref, "oid": oid, "identity": identity}
    return {"status": "conflict", "reason": "exists", "ref": ref, "oid": current, "identity": identity}


def delete_exact(repo: Path, ref: str, oid: str, *, budget: GitBudget | None = None) -> dict:
    """Delete only when the ref still names ``oid``. Any other result leaks the ref."""
    if not ref.startswith(CUSTODY_PREFIX + "/") or _OID.fullmatch(oid) is None:
        return {"status": "invalid", "ref": ref}
    symbol = run_git(repo, ["symbolic-ref", "-q", ref], budget=budget)
    if symbol["status"] != "ok" or symbol["rc"] != 1:
        return {"status": "leaked", "reason": "symbolic_or_unreadable", "ref": ref}
    result = run_git(repo, ["update-ref", "--no-deref", "-d", ref, oid], budget=budget, cap=1.0)
    if result["status"] != "ok":
        return {"status": "unknown", "reason": result["reason"], "ref": ref}
    if result["rc"] == 0:
        return {"status": "deleted", "ref": ref}
    return {"status": "leaked", "ref": ref}


def verify_snapshot(repo: Path, oid: str, *, budget: GitBudget | None = None) -> dict:
    """Committed tree/blob snapshot. ``?OID`` on exit 0 is incomplete. Alternates stay pending."""
    if _OID.fullmatch(oid) is None:
        return {"status": "invalid", "missing": [], "submodules": [], "reason": "oid"}
    if os.environ.get("GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        return {"status": "pending", "missing": [], "submodules": [], "reason": "alternates"}
    alt = _git_path(repo, "objects/info/alternates", budget=budget)
    if alt is None:
        return {"status": "unknown", "missing": [], "submodules": [], "reason": "alternates_unreadable"}
    try:
        text = read_regular(alt, max_bytes=GIT_INPUT_MAX_BYTES).decode("utf-8-sig")
    except FileNotFoundError:
        text = ""
    except (OSError, ValueError):
        return {"status": "pending", "missing": [], "submodules": [], "reason": "alternates"}
    if text.strip():
        return {"status": "pending", "missing": [], "submodules": [], "reason": "alternates"}
    listed = run_git(repo, ["rev-list", "--objects", "--missing=print", "--no-walk", oid], budget=budget)
    if listed["status"] != "ok":
        return {"status": "unknown", "missing": [], "submodules": [], "reason": listed["reason"]}
    missing = [line[1:].split()[0] for line in listed["stdout"].splitlines() if line.startswith("?")]
    if missing or listed["rc"] != 0:
        return {"status": "incomplete", "missing": missing, "submodules": [], "reason": "missing_object"}
    oids = [line.split()[0] for line in listed["stdout"].splitlines() if line and not line.startswith("?")]
    checked = run_git(
        repo, ["cat-file", "--batch-check=%(objecttype) %(objectname)"],
        budget=budget, stdin_text="".join(f"{item}\n" for item in oids))
    if checked["status"] != "ok" or checked["rc"] != 0:
        return {"status": "unknown", "missing": [], "submodules": [], "reason": "batch_check"}
    lines = [line for line in checked["stdout"].splitlines() if line]
    if len(lines) != len(oids):
        return {"status": "incomplete", "missing": [oid], "submodules": [], "reason": "batch_check"}
    for line in lines:
        kind, sep, name = line.partition(" ")
        if not sep or kind in {"missing", "ambiguous"} or name in {"missing", "ambiguous"}:
            return {"status": "incomplete", "missing": [name or kind or oid], "submodules": [],
                    "reason": "missing_object"}
        if kind not in {"commit", "tree", "blob", "tag"}:
            return {"status": "incomplete", "missing": [name], "submodules": [], "reason": "object_type"}
    tree = run_git(repo, ["ls-tree", "-r", oid], budget=budget)
    if tree["status"] != "ok" or tree["rc"] != 0:
        return {"status": "unknown", "missing": [], "submodules": [], "reason": "ls-tree"}
    submodules = []
    for line in tree["stdout"].splitlines():
        if not line.startswith("160000 "):
            continue
        meta, _sep, path = line.partition("\t")
        parts = meta.split()
        submodules.append({"path": path, "oid": parts[-1] if parts else ""})
    if submodules:
        return {"status": "incomplete", "missing": [], "submodules": submodules, "reason": "submodule"}
    return {"status": "complete", "missing": [], "submodules": [], "reason": None}


def capture_reflog(repo: Path, *, lower_bound: float, budget: GitBudget | None = None) -> dict:
    """Both nonzero old/new OIDs at or after ``lower_bound``. One bad line stays pending."""
    path = _git_path(repo, "logs/HEAD", budget=budget)
    if path is None:
        return {"status": "unknown", "oids": [], "reason": "reflog_path"}
    try:
        raw = read_regular(path, max_bytes=GIT_INPUT_MAX_BYTES)
    except FileNotFoundError:
        return {"status": "pending", "oids": [], "reason": "missing_reflog"}
    except (OSError, ValueError):
        return {"status": "pending", "oids": [], "reason": "unreadable_reflog"}
    try:
        lines = raw.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError:
        return {"status": "pending", "oids": [], "reason": "unparseable"}
    oids: list[str] = []
    bad = False
    for line in lines:
        if not line.strip():
            continue
        parsed = _REFLOG.match(line)
        if parsed is None:
            bad = True
            continue
        old, new, stamp = parsed.group(1), parsed.group(2), int(parsed.group(3))
        if stamp < lower_bound:
            continue
        for item in (old, new):
            if set(item) != {"0"} and item not in oids:
                oids.append(item)
    if bad:
        return {"status": "pending", "oids": oids, "reason": "unparseable"}
    return {"status": "ok", "oids": oids, "reason": None}


def restore_ref(source: Path, dest: Path, ref: str, *, budget: GitBudget | None = None) -> dict:
    """Import objects, then create one custody ref with an expected-absent CAS.

    Fetch must not mutate destination refs or FETCH_HEAD. Not a publication proof.
    """
    if not ref.startswith(CUSTODY_PREFIX + "/"):
        return {"status": "invalid", "reason": "namespace", "ref": ref}
    checked = run_git(source, ["check-ref-format", ref], budget=budget)
    if checked["status"] != "ok" or checked["rc"] != 0:
        return {"status": "invalid", "reason": "ref", "ref": ref}
    oid = _read_ref(source, ref, budget=budget)
    if oid is None:
        return {"status": "unknown", "reason": "source_ref", "ref": ref}
    fetched = run_git(dest, ["fetch", "--no-tags", "--no-write-fetch-head", "--refmap=",
                             "--no-recurse-submodules", "--", str(source), oid], budget=budget)
    if fetched["status"] != "ok":
        return {"status": "unknown", "reason": fetched["reason"], "ref": ref}
    if fetched["rc"] != 0:
        return {"status": "unknown", "reason": "fetch", "ref": ref}
    created = create_ref(dest, ref, oid, budget=budget)
    if created["status"] != "created":
        return created
    return {"status": "restored", "ref": ref}
