"""Registered clones for workspace_source v1: the proof, the GitHub id cache and the Board attestation (plan v2 §3,
v3 S5). Only runs when ``kanban.workspace_source.enabled`` (refused until the protocol is complete, so inert).

A clone is proven when all hold: the path is a repo root; the raw ``remote.origin.url`` canonicalizes to a GitHub URL;
``ls-remote --get-url origin`` returns the same URL and there is no ``pushurl`` and no ``url.*`` rewrite; ``ls-remote
origin HEAD`` succeeds in BatchMode within 5 s; the GitHub API id for the canonical name matches (renames follow the
id: matching is always by id). The id is cached per clone for 6 h in ``<kanban home>/source-proof.json``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_cli import kanban_workspace_source as _kws
from hermes_cli.kanban_workspace_source import Settings, WorkspaceSourceRefusal

_log = logging.getLogger(__name__)

ID_CACHE_SECONDS = 6 * 3600
LS_REMOTE_TIMEOUT_S = 5
MAX_ATTESTED = 32
_GITHUB = (
    re.compile(r"https://github\.com/(?P<o>[A-Za-z0-9_.-]+)/(?P<n>[A-Za-z0-9_.-]+?)(?:\.git)?/?"),
    re.compile(r"(?:ssh://)?git@github\.com[:/](?P<o>[A-Za-z0-9_.-]+)/(?P<n>[A-Za-z0-9_.-]+?)(?:\.git)?/?"),
)
_BATCH_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oConnectTimeout=5",
              "GCM_INTERACTIVE": "never"}


class ProofFailed(WorkspaceSourceRefusal):
    """A registered clone failed its proof (host scope: ``clone_missing``/``origin_rewritten``/``wrong_repo``/
    ``origin_unreachable``)."""


def canonical_github(url: str) -> Optional[str]:
    """``<owner>/<name>`` for a GitHub remote URL, else None (a non-GitHub origin is never portable)."""
    for pattern in _GITHUB:
        if m := pattern.fullmatch(url.strip()):
            return f"{m['o']}/{m['n']}"
    return None


def git(path: str | Path, *args: str, timeout: float = 30, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, **_BATCH_ENV}
    try:
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=timeout,
                              check=check, env=env)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"git {' '.join(args)} timed out after {timeout}s") from exc


def github_repo(full_name: str) -> dict:
    """``{id, full_name}`` from ``GET /repos/{owner}/{name}`` (follows renames) via the gh CLI and its broker token.
    Raises on any failure: no API access means no proof (fail closed)."""
    out = subprocess.run(["gh", "api", f"repos/{full_name}", "--jq", "{id: .id, full_name: .full_name}"],
                         capture_output=True, text=True, timeout=15, check=True)
    data = json.loads(out.stdout)
    if not isinstance(data.get("id"), int) or not isinstance(data.get("full_name"), str):
        raise ValueError(f"unexpected GitHub API answer for {full_name}")
    return data


def _cache_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "kanban" / "source-proof.json"


def _cached_repo(path: str, full_name: str, lookup: Callable[[str], dict], now: float) -> dict:
    cache_file = _cache_path()
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    hit = cache.get(path) if isinstance(cache, dict) else None
    if isinstance(hit, dict) and hit.get("name") == full_name and now - float(hit.get("at", 0)) < ID_CACHE_SECONDS:
        return {"id": hit["id"], "full_name": hit["full_name"]}
    repo = lookup(full_name)
    cache = cache if isinstance(cache, dict) else {}
    cache[path] = {"name": full_name, "id": repo["id"], "full_name": repo["full_name"], "at": now}
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, sort_keys=True), encoding="utf-8")
    tmp.replace(cache_file)
    return repo


@dataclass(frozen=True)
class ProvenClone:
    key: str  # the configured ``<owner>/<name>``
    path: str
    repository_id: int
    full_name: str  # GitHub's current name (renames follow the id)


def _live_checkout() -> Optional[Path]:
    try:
        return Path(__file__).resolve().parents[1]
    except IndexError:
        return None


def prove_clone(key: str, path: str, *, expected_id: Optional[int] = None,
                lookup: Optional[Callable[[str], dict]] = None, now: Optional[float] = None) -> ProvenClone:
    """Prove one registered clone; raises :class:`ProofFailed` with a host-scope code."""
    now = time.time() if now is None else now
    lookup = lookup or github_repo
    root = Path(path).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ProofFailed("clone_missing", f"{path} is not an absolute directory")
    live = _live_checkout()
    if live is not None and root.resolve() == live:
        raise ProofFailed("clone_missing", f"{path} is the live checkout; register a dedicated clone")
    try:
        top = git(root, "rev-parse", "--show-toplevel").stdout.strip()
    except (subprocess.CalledProcessError, TimeoutError) as exc:
        raise ProofFailed("clone_missing", f"{path} is not a git repository ({exc})") from exc
    if Path(top).resolve() != root.resolve():
        raise ProofFailed("clone_missing", f"{path} is not a repository root ({top})")
    raw = git(root, "config", "--get", "remote.origin.url", check=False).stdout.strip()
    full_name = canonical_github(raw) if raw else None
    if full_name is None:
        raise ProofFailed("wrong_repo", f"origin {raw or '(none)'} is not a GitHub repository")
    effective = git(root, "ls-remote", "--get-url", "origin", check=False).stdout.strip()
    pushurl = git(root, "config", "--get-all", "remote.origin.pushurl", check=False).stdout.strip()
    rewrites = git(root, "config", "--get-regexp", r"^url\.", check=False).stdout.strip()
    if effective != raw or pushurl or rewrites:
        raise ProofFailed("origin_rewritten", f"origin {raw} resolves to {effective or '?'}"
                          + (f", pushurl {pushurl}" if pushurl else "") + (" (url.* rewrite set)" if rewrites else ""))
    try:
        git(root, "ls-remote", "origin", "HEAD", timeout=LS_REMOTE_TIMEOUT_S)
    except (subprocess.CalledProcessError, TimeoutError) as exc:
        raise ProofFailed("origin_unreachable", f"ls-remote origin HEAD failed: {exc}") from exc
    try:
        repo = _cached_repo(str(root.resolve()), full_name, lookup, now)
    except Exception as exc:  # no API access: no proof, not portable (fail closed)
        raise ProofFailed("origin_unreachable", f"GitHub API lookup of {full_name} failed: {exc}") from exc
    # A rename keeps the id: a configured key that is the old (or new) name resolves to the same repository.
    keyed = _cached_repo(f"key:{key}", key, lookup, now) if key.lower() != full_name.lower() else repo
    if keyed["id"] != repo["id"] or (expected_id is not None and repo["id"] != expected_id):
        raise ProofFailed("wrong_repo", f"{path} is GitHub repo {repo['id']} ({repo['full_name']}), expected "
                          f"{expected_id if expected_id is not None else keyed['id']}")
    return ProvenClone(key=key, path=str(root.resolve()), repository_id=int(repo["id"]), full_name=repo["full_name"])


def proven_clones(cfg: Optional[Settings] = None, *, lookup: Optional[Callable[[str], dict]] = None) -> list[ProvenClone]:
    """Every configured clone that proves now; a failing one is logged and left out (never attested)."""
    cfg = cfg or _kws.settings()
    if not cfg.enabled:
        return []
    out = []
    for key, path in sorted(cfg.clones.items()):
        try:
            out.append(prove_clone(key, path, lookup=lookup))
        except ProofFailed as exc:
            _log.warning("workspace_source: clone %s at %s not proven: %s", key, path, exc)
    return out


def proven_repo_ids(cfg: Optional[Settings] = None) -> set[int]:
    return {c.repository_id for c in proven_clones(cfg)}


def clone_for(repository_id: int, cfg: Settings, *, lookup: Optional[Callable[[str], dict]] = None) -> ProvenClone:
    """The registered clone for ``repository_id``, proof re-run now (``clone_missing``/``wrong_repo``/...)."""
    last: Optional[ProofFailed] = None
    for key, path in sorted(cfg.clones.items()):
        try:
            clone = prove_clone(key, path, lookup=lookup)
        except ProofFailed as exc:
            last = exc
            continue
        if clone.repository_id == repository_id:
            return clone
    if last is not None and len(cfg.clones) == 1:
        raise last
    raise ProofFailed("clone_missing", f"no proven registered clone for GitHub repo {repository_id}")


# --- attestation ---------------------------------------------------------------------------------------------------

_BOARD_LACKS_ROUTE = False


def board_lacks_source_route() -> bool:
    return _BOARD_LACKS_ROUTE


def attest(post: Callable[[str, dict], Any], clones: list[ProvenClone]) -> bool:
    """``POST /v2/machines/me/sources {version: 1, repos: [{id, full_name}]}`` (at most 32; an empty list clears this
    machine's entries). A 404 from an old Board is logged once and disables portability locally
    (``board_lacks_source_route``). Returns whether the Board took the attestation."""
    global _BOARD_LACKS_ROUTE
    if _BOARD_LACKS_ROUTE:
        return False
    repos = [{"id": c.repository_id, "full_name": c.full_name} for c in clones][:MAX_ATTESTED]
    try:
        post("/v2/machines/me/sources", {"version": 1, "repos": repos})
    except Exception as exc:
        if getattr(exc, "status", None) == 404:
            _BOARD_LACKS_ROUTE = True
            _log.warning("workspace_source: the Board has no /v2/machines/me/sources route "
                         "(board_lacks_source_route); portability disabled on this host")
            return False
        _log.info("workspace_source: attestation failed: %s", exc)
        return False
    return True


def attest_heartbeat(post: Callable[[str, dict], Any], cfg: Optional[Settings] = None) -> bool:
    """The registry-heartbeat hook: prove and attest, only when enabled (a disabled client stops refreshing, so its
    entries expire on the Board after 15 min)."""
    cfg = cfg or _kws.settings()
    if not cfg.enabled or board_lacks_source_route():
        return False
    return attest(post, proven_clones(cfg))


ATTEST_EVERY_S = 300  # well inside the Board's 15 min attestation expiry
_ATTEST_STATE = {"next_at": 0.0}


def attest_if_due(hub: Any, *, now: Optional[float] = None, cfg: Optional[Settings] = None) -> bool:
    """Once per :data:`ATTEST_EVERY_S` from the sync round; never raises into it. Inert while disabled."""
    clock = time.monotonic() if now is None else now
    if clock < _ATTEST_STATE["next_at"]:
        return False
    cfg = cfg or _kws.settings()
    if not cfg.enabled:
        return False
    poster = getattr(hub, "attest_sources", None)
    if poster is None:
        return False
    _ATTEST_STATE["next_at"] = clock + ATTEST_EVERY_S
    try:
        return attest_heartbeat(lambda _path, body: poster(body), cfg)
    except Exception as exc:  # proof errors are per-clone; anything else must not break the sync round
        _log.warning("workspace_source: attestation round failed: %s", exc)
        return False
