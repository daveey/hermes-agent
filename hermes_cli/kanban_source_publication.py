"""Inert publication proof and refspec refusal. Does not push or activate custody.

A completed negative is not an empty inventory. A timeout, unfetched tip or
local filesystem remote is not cross-host publication. Stale tracking refs,
grafts and replace refs are not evidence.
"""
from __future__ import annotations

from pathlib import Path

from hermes_cli import kanban_source_refs as refs
from hermes_cli.kanban_source_refs import GitBudget

_NS = "refs/hermes-source-custody"
TEST_LOCAL_REMOTE = "hermes-test-local-remote"


def reject_refspec(spec: str) -> str | None:
    """Refuse the custody namespace, including an abbreviated name, and any glob."""
    body = spec.removeprefix("+")
    if body in {"*", "--mirror"} or spec == "--mirror":
        return "broad"
    src, sep, dst = body.partition(":")
    # ``:refs/heads/x`` is one explicit deletion: its empty source is not a broad selector.
    sides = ([dst] if dst and not src else [src, dst]) if sep else [src]
    for side in sides:
        if not side or "*" in side:
            return "broad"
        normalized = side
        if side == "hermes-source-custody" or side.startswith("hermes-source-custody/"):
            normalized = "refs/" + side
        if normalized == _NS or normalized.startswith(_NS + "/") or _NS in normalized:
            return "custody_namespace"
    return None


def _mirror_value_enabled(value: str) -> bool:
    """Conservative legacy resolver veto; the publication gate uses Git's bool parser."""
    return value.strip().lower() not in {"", "false", "no", "off", "0"}


def _config(repo: Path, config: list[str], *args: str, budget=None):
    result = refs.run_git(repo, [*config, "config", *args], budget=budget)
    if result["status"] != "ok" or result["rc"] not in (0, 1):
        return None
    return result["stdout"].rstrip("\n") if result["rc"] == 0 else ""


def _default_target(repo: Path, config: list[str], budget=None) -> str | None:
    branch = refs.run_git(repo, [*config, "symbolic-ref", "--quiet", "--short", "HEAD"], budget=budget)
    if branch["status"] != "ok" or branch["rc"] not in (0, 1):
        return None
    name = branch["stdout"].strip()
    keys = ([f"branch.{name}.pushRemote"] if name else []) + ["remote.pushDefault"]
    if name:
        keys.append(f"branch.{name}.remote")
    for key in keys:
        value = _config(repo, config, "--get", key, budget=budget)
        if value is None or value:
            return value
    return "origin"


def _path_target(target: str) -> bool:
    return target == "." or "/" in target or ":" in target or "\\" in target


def reject_configured_push(repo: Path, *, budget: GitBudget | None = None,
                           remote: str | None = None, config: list[str] | None = None) -> str | None:
    """Check ONLY Git's selected target. Use Git itself for config boolean semantics."""
    config = config or []
    remote = remote if remote is not None else _default_target(repo, config, budget)
    if not remote:
        return "unknown"
    # A configured named remote wins over a same-named path (names may contain '/').
    names = refs.run_git(repo, [*config, "remote"], budget=budget)
    if names["status"] != "ok" or names["rc"] != 0:
        return "unknown"
    if remote not in names["stdout"].splitlines():
        if _path_target(remote):
            return reject_refspec(remote)
        return None if (repo / remote).exists() else "unknown"
    mirror = _config(repo, config, "--type=bool", "--get", f"remote.{remote}.mirror", budget=budget)
    if mirror is None:
        return "unknown"
    if mirror == "true":
        return "mirror"
    push = _config(repo, config, "--get-all", f"remote.{remote}.push", budget=budget)
    if push is None:
        return "unknown"
    for spec in push.splitlines():
        if refused := reject_refspec(spec):
            return refused
    return None


def reject_publish_argv(argv: list[str], *, repo: Path | None = None,
                        remote: str | None = None) -> str | None:
    """Parse once: globals, options, target, then refspecs. No data is a config key."""
    from scripts.ci.upstream_sync_push_argv import parse
    push = parse(argv)
    if push.refusal:
        return push.refusal
    if push.mirror:
        return "mirror"
    for arg in ([push.target] if push.target else []) + push.specs:
        if refused := reject_refspec(arg):
            return refused
    target = push.target
    if target is None and repo is not None:
        target = _default_target(repo, push.config)
    if remote is not None and remote != target:
        return "unknown"
    if repo is not None:
        if target is None:
            return "unknown"
        return reject_configured_push(repo, remote=target, config=push.config)
    # Lexical callers have no repo: only explicitly supplied target config applies.
    if push.config:
        if not target:
            return "unknown"
        if not _path_target(target):
            mirror = _config(Path.cwd(), push.config, "--type=bool", "--get", f"remote.{target}.mirror")
            if mirror is None:
                return "unknown"
            if mirror == "true":
                return "mirror"
    return None


def remote_transport(url: str) -> str:
    """``local`` is a filesystem path. SCP ``host:path`` is network.

    A slash before the first colon is a local path. Git does not treat that
    as an SSH URL, so it must not count as cross-host publication.
    """
    lowered = url.lower()
    if lowered.startswith(("https://", "http://", "ssh://", "git://")):
        return "network"
    if lowered.startswith("file://"):
        return "local"
    colon = url.find(":")
    if colon > 0 and "/" not in url[:colon]:
        return "network"
    return "local"


def _tips(text: str) -> list[tuple[str, str]]:
    found = []
    for line in text.splitlines():
        oid, sep, ref = line.partition("\t")
        if sep and ref.startswith("refs/heads/") and refs._OID.fullmatch(oid):
            found.append((oid, ref))
    return found


def _ancestor(repo: Path, oid: str, tip: str, budget: GitBudget | None) -> str:
    checked = refs.run_git(repo, ["merge-base", "--is-ancestor", oid, tip], budget=budget)
    if checked["status"] != "ok":
        return "unknown"
    if checked["rc"] == 0:
        return "yes"
    if checked["rc"] == 1:
        return "no"
    return "unknown"


def prove(repo: Path, oid: str, *, allow_local_remote: bool | str = False,
          budget: GitBudget | None = None) -> dict:
    """Advertised tip equality or ancestry. ``release`` is false unless status is published."""
    if refs._OID.fullmatch(oid) is None:
        return {"status": "invalid", "release": False, "reason": "oid"}
    listed = refs.run_git(repo, ["remote"], budget=budget)
    if listed["status"] != "ok":
        return {"status": "unknown", "release": False, "reason": listed["reason"]}
    if listed["rc"] != 0:
        return {"status": "unknown", "release": False, "reason": "remote"}
    names = [item for item in listed["stdout"].split() if item]
    if not names:
        return {"status": "not_published", "release": False, "reason": "no_remote"}
    urls = []
    for name in names:
        url = refs.run_git(repo, ["remote", "get-url", name], budget=budget)
        if url["status"] != "ok" or url["rc"] != 0:
            return {"status": "unknown", "release": False, "reason": "remote_url"}
        urls.append((name, url["stdout"].strip()))
    if any(remote_transport(url) == "local" for _name, url in urls) and allow_local_remote != TEST_LOCAL_REMOTE:
        return {"status": "not_published", "release": False, "reason": "local_remote"}
    tips: list[tuple[str, str, str]] = []
    for name, _url in urls:
        advertised = refs.run_git(repo, ["ls-remote", "--heads", name], budget=budget)
        if advertised["status"] != "ok":
            return {"status": "unknown", "release": False, "reason": advertised["reason"]}
        if advertised["rc"] != 0:
            return {"status": "unknown", "release": False, "reason": "ls-remote"}
        tips.extend((name, tip, ref) for tip, ref in _tips(advertised["stdout"]))
    if any(tip == oid for _name, tip, _ref in tips):
        return {"status": "published", "release": True, "reason": "advertised_tip"}
    unknown = False
    fetched = False
    for name, tip, ref in tips:
        verdict = _ancestor(repo, oid, tip, budget)
        if verdict == "yes":
            return {"status": "published", "release": True, "reason": "advertised_ancestor"}
        if verdict == "unknown" and not fetched:
            fetched = True
            brought = refs.run_git(
                repo, ["fetch", "--no-tags", "--no-write-fetch-head", "--refmap=",
                       "--no-recurse-submodules", "--", name, tip], budget=budget)
            if brought["status"] != "ok" or brought["rc"] != 0:
                unknown = True
                continue
            verdict = _ancestor(repo, oid, tip, budget)
            if verdict == "yes":
                return {"status": "published", "release": True, "reason": "advertised_ancestor"}
            unknown = unknown or verdict != "no"
        elif verdict == "unknown":
            unknown = True
    if unknown:
        return {"status": "unknown", "release": False, "reason": "unfetched_tip"}
    return {"status": "not_published", "release": False, "reason": "not_advertised"}
