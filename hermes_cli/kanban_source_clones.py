"""Registered clones for workspace_source v1: the proof, the GitHub id cache and the Board attestation (plan v2 §3,
v3 S5). Only runs when ``kanban.workspace_source.enabled`` (refused until the protocol is complete, so inert).

A clone is proven when all hold: the path is a repo root; the raw ``remote.origin.url`` canonicalizes to a GitHub URL;
``ls-remote --get-url origin`` and ``remote get-url --push origin`` return the same URL, there is no ``pushurl``
and the clone's own config has no ``url.*`` rule (a global one must leave both URLs unchanged); ``ls-remote
origin HEAD`` succeeds in BatchMode within 5 s; the GitHub API id for the canonical name matches (renames follow the
id: matching is always by id). The id is cached per clone for 6 h in ``<kanban home>/source-proof.json``.
"""

from __future__ import annotations

import json
import logging
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
    from tools.environments.local import build_subprocess_env
    env = build_subprocess_env(scrub_secrets=False, inherit_profile_home=False, extra=_BATCH_ENV)
    try:
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, check=check, env=env)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"git {' '.join(args)} timed out after {timeout}s") from exc


def github_repo(full_name: str, *, timeout: float = 30) -> dict:
    """``{id, full_name}`` from ``GET /repos/{owner}/{name}`` (follows renames) via the gh CLI and its broker token.
    Raises on any failure: no API access means no proof (fail closed).

    The argv is the dispatcher's (:func:`hermes_cli.kanban_gh_argv.dispatcher_gh_argv`: the home's
    ``kanban.pr_acceptance.gh_command_prefix`` broker wrapper + a resolved ``gh``). The hub's gateway and its card
    runs have no ambient ``gh`` login or GH_TOKEN, so a bare ``gh api`` failed there and no hub clone ever proved:
    every hub-held card stayed ``retained`` (t_01a1cc30806b0e62 rollout)."""
    from hermes_cli.kanban_gh_argv import dispatcher_gh_argv
    argv = [*dispatcher_gh_argv(), "api", f"repos/{full_name}", "--jq", "{id: .id, full_name: .full_name}"]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=timeout, check=True)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"gh api repos/{full_name} timed out after {timeout}s") from exc
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
        cache = json.loads(cache_file.read_text(encoding="utf-8-sig"))
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
    from hermes_cli._startup_fast import project_root_str
    return Path(project_root_str()).resolve()


def prove_clone(key: str, path: str, *, expected_id: Optional[int] = None,
                lookup: Optional[Callable[[str], dict]] = None, now: Optional[float] = None,
                budget: Optional[Callable[[float], float]] = None) -> ProvenClone:
    """Prove one registered clone; raises :class:`ProofFailed` with a host-scope code.

    ``budget(cap)`` returns the seconds a call may take under a caller's deadline (it raises once the deadline is
    spent); every git call and the default GitHub lookup are capped by it. Without it each call keeps its own cap."""
    now = time.time() if now is None else now
    cap = budget or (lambda seconds: seconds)
    if lookup is None:
        def lookup(full_name: str) -> dict:
            return github_repo(full_name, timeout=cap(30))
    root = Path(path).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ProofFailed("clone_missing", f"{path} is not an absolute directory")
    live = _live_checkout()
    if live is not None and root.resolve() == live:
        raise ProofFailed("clone_missing", f"{path} is the live checkout; register a dedicated clone")
    try:
        top = git(root, "rev-parse", "--show-toplevel", timeout=cap(30)).stdout.strip()
    except (subprocess.CalledProcessError, TimeoutError) as exc:
        raise ProofFailed("clone_missing", f"{path} is not a git repository ({exc})") from exc
    if Path(top).resolve() != root.resolve():
        raise ProofFailed("clone_missing", f"{path} is not a repository root ({top})")
    raw = git(root, "config", "--get", "remote.origin.url", timeout=cap(30), check=False).stdout.strip()
    full_name = canonical_github(raw) if raw else None
    if full_name is None:
        raise ProofFailed("wrong_repo", f"origin {raw or '(none)'} is not a GitHub repository")
    effective = git(root, "ls-remote", "--get-url", "origin", timeout=cap(30), check=False).stdout.strip()
    push_effective = git(root, "remote", "get-url", "--push", "origin", timeout=cap(30), check=False).stdout.strip()
    pushurl = git(root, "config", "--get-all", "remote.origin.pushurl", timeout=cap(30), check=False).stdout.strip()
    # Any url.* rule in the clone's OWN config refuses. A global/system rule counts only when it changes this
    # origin's fetch or push URL (both expanded above): every pool worker carries install-push-route's
    # ``url.https://github.com/.pushInsteadOf git@github.com:``, which never touches an https origin, and refusing
    # it made no worker prove a clone (t_01a1cc30806b0e62 rollout).
    rewrites = git(root, "config", "--local", "--get-regexp", r"^url\.", timeout=cap(30), check=False).stdout.strip()
    if effective != raw or push_effective != raw or pushurl or rewrites:
        raise ProofFailed("origin_rewritten", f"origin {raw} resolves to {effective or '?'}"
                          + (f", pushes to {push_effective or '?'}" if push_effective != raw else "")
                          + (f", pushurl {pushurl}" if pushurl else "") + (" (url.* rewrite set)" if rewrites else ""))
    try:
        git(root, "ls-remote", "origin", "HEAD", timeout=cap(LS_REMOTE_TIMEOUT_S))
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
    except Exception as exc:  # health: allow BLE001 -- Board post boundary: any failure means "not attested"
        if getattr(exc, "status", None) == 404:
            _BOARD_LACKS_ROUTE = True
            _log.warning("workspace_source: the Board has no /v2/machines/me/sources route "
                         "(board_lacks_source_route); portability disabled on this host")
            return False
        _log.warning("workspace_source: attestation failed: %s", exc, exc_info=True)
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
    except Exception as exc:  # health: allow BLE001 -- sync-round boundary; logged with traceback
        _log.warning("workspace_source: attestation round failed: %s", exc, exc_info=True)
        return False
