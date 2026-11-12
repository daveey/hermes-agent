"""workspace_source v1 (core PR-1, inert): manifest + custody parsing, the grant snapshot, resolver refusals, the
host-local skip memo, the pickup skip and the ``workspace_kind``/``branch_name`` fence.

Spec: daveey/hermes ``docs/designs/workspace-source-v1`` (plan v2 §1/§3, v3 §2/§6/§6b, v4 R4b/R4c/S9, v4.1).
Everything here is gated on ``kanban.workspace_source.enabled``, which is refused until ``SOURCE_PROTOCOL_COMPLETE``
(core PR-2 flips it): until then no grant request carries ``source_protocol`` and the Board never grants a custody or
manifest card to this core, so the routing below only ever sees snapshots with no source.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from hermes_cli.kanban_workspace_source_config import DEFAULTS

_log = logging.getLogger(__name__)

# Core PR-2 (preflight/publish, attach + pre-spawn abort, source_protocol) flips this. Until then ``enabled: true``
# is refused: a half-built client must never ask the Board for custody cards (v4 S9).
SOURCE_PROTOCOL_COMPLETE = False

MANIFEST_MAX_BYTES = 8 * 1024
CUSTODY_STATES = frozenset({"attached", "published", "unchanged", "retained"})
PORTABLE_STATES = frozenset({"published", "unchanged"})
_HEX40 = re.compile(r"[0-9a-f]{40}")
_FULL_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_BRANCH = re.compile(r"refs/heads/(?!.*\.\.)(?!/)[A-Za-z0-9._/-]+(?<![/.])")
_MANIFEST_KEYS = frozenset({"version", "repository", "branch", "commit", "base_commit", "published_at",
                            "manifest_revision", "published_by", "source_kind", "artifacts"})
_REPO_KEYS = frozenset({"id", "full_name", "url"})
_BY_KEYS = frozenset({"run_uid", "machine"})
SOURCE_KINDS = frozenset({"task_worktree", "scratch_external"})

# v3 §6b: host scope releases the grant and memoizes ``(task, manifest_revision)``; card scope releases and blocks.
HOST_SCOPE = frozenset({"wrong_repo", "origin_rewritten", "origin_unreachable", "occupied_path", "dirty_destination",
                        "diverged_local_branch", "clone_missing", "materialize_timeout", "custody_not_portable",
                        "source_disabled"})
CARD_SCOPE = frozenset({"remote_drift", "remote_branch_missing", "manifest_invalid", "custody_source_missing"})
FENCED_COLUMNS = frozenset({"workspace_kind", "branch_name"})
SKIP_MEMO_SECONDS = 6 * 3600


class ManifestInvalid(ValueError):
    """A ``workspace_source`` manifest outside the strict v1 schema (unknown key or version, oversized, ...)."""


class WorkspaceSourceRefusal(Exception):
    """The resolver refused a source card here. ``scope`` is ``host`` (another host or the holder may still run it)
    or ``card`` (the source moved or vanished outside a lease: a human decides). Never a
    ``WorktreeWorkspaceConfigError``: a refusal must not force-trip a shared card."""

    def __init__(self, code: str, detail: str = "", *, evidence: Optional[dict] = None):
        if code not in HOST_SCOPE | CARD_SCOPE:
            raise ValueError(f"unknown workspace_source refusal code {code!r}")
        self.code, self.scope = code, ("host" if code in HOST_SCOPE else "card")
        self.detail, self.evidence = detail, dict(evidence or {})
        super().__init__(f"{code}: {detail}" if detail else code)


class SourceFenced(ValueError):
    """A local edit of ``workspace_kind``/``branch_name`` on a card with custody or a manifest (R4c)."""

    code = "source_fenced"


# --- settings ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    enabled: bool
    requested: bool
    clones: dict[str, str]
    publish_on_park: bool
    push_timeout_s: float
    preflight_timeout_s: float
    materialize_timeout_s: Optional[float]

    def materialize_bound(self, ttl_s: Optional[float]) -> float:
        if self.materialize_timeout_s is not None:
            return self.materialize_timeout_s
        return max((ttl_s or 900) / 3 - 30, 30.0)


_WARNED: set[str] = set()


def settings(config: Optional[Mapping[str, Any]] = None) -> Settings:
    """``kanban.workspace_source`` merged over :data:`DEFAULTS`. ``enabled: true`` is refused with a warning (and
    treated false) until :data:`SOURCE_PROTOCOL_COMPLETE`."""
    if config is None:
        try:
            from hermes_cli.config import load_config
            config = load_config() or {}
        except Exception:  # a broken config never enables anything
            config = {}
    raw = ((config.get("kanban") or {}).get("workspace_source") or {}) if isinstance(config, Mapping) else {}
    merged = {**DEFAULTS, **(raw if isinstance(raw, Mapping) else {})}
    requested = merged.get("enabled") is True
    if requested and not SOURCE_PROTOCOL_COMPLETE and "protocol" not in _WARNED:
        _WARNED.add("protocol")
        _log.warning("kanban.workspace_source.enabled is true but this core does not implement the full source "
                     "protocol yet (SOURCE_PROTOCOL_COMPLETE=False): treated as false")
    clones = merged.get("clones")
    clones = clones if isinstance(clones, Mapping) else {}
    push = float(merged.get("push_timeout_s") or DEFAULTS["push_timeout_s"])
    pre = merged.get("preflight_timeout_s")
    mat = merged.get("materialize_timeout_s")
    return Settings(
        enabled=requested and SOURCE_PROTOCOL_COMPLETE, requested=requested,
        clones={str(k): str(v) for k, v in clones.items()},
        publish_on_park=merged.get("publish_on_park") is not False, push_timeout_s=push,
        preflight_timeout_s=float(pre) if pre is not None else push + 30,
        materialize_timeout_s=float(mat) if mat is not None else None,
    )


# --- manifest + custody --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Manifest:
    repository_id: int
    full_name: str
    url: str
    branch: str  # ``refs/heads/<b>``
    commit: str
    base_commit: Optional[str]
    published_at: int
    manifest_revision: int
    published_by: dict
    source_kind: str
    raw: dict = field(compare=False, repr=False)

    @property
    def short_branch(self) -> str:
        return self.branch[len("refs/heads/"):]


def _decode(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        return json.loads(value)
    return value


def _int(value: Any, what: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ManifestInvalid(f"{what} must be an integer >= {minimum}")
    return value


def parse_manifest(value: Any) -> Manifest:
    """Strict v1 schema (plan v2 §1, v4 §0): unknown keys and versions are refused, no paths, at most 8 KiB, and
    ``artifacts`` is reserved (must be ``[]`` in v1)."""
    try:
        size = len(value if isinstance(value, (bytes, bytearray)) else
                   (value if isinstance(value, str) else json.dumps(value)).encode("utf-8"))
        data = _decode(value)
    except (TypeError, ValueError) as exc:
        raise ManifestInvalid(f"not JSON: {exc}") from exc
    if size > MANIFEST_MAX_BYTES:
        raise ManifestInvalid(f"manifest is {size} bytes (max {MANIFEST_MAX_BYTES})")
    if not isinstance(data, dict):
        raise ManifestInvalid("manifest must be an object")
    if unknown := set(data) - _MANIFEST_KEYS:
        raise ManifestInvalid(f"unknown keys {sorted(unknown)}")
    if missing := _MANIFEST_KEYS - set(data):
        raise ManifestInvalid(f"missing keys {sorted(missing)}")
    if data["version"] != 1 or isinstance(data["version"], bool):
        raise ManifestInvalid(f"unsupported version {data['version']!r}")
    repo, by = data["repository"], data["published_by"]
    if not isinstance(repo, dict) or set(repo) != _REPO_KEYS:
        raise ManifestInvalid("repository must be {id, full_name, url}")
    repo_id = _int(repo["id"], "repository.id", 1)
    if not isinstance(repo["full_name"], str) or not _FULL_NAME.fullmatch(repo["full_name"]):
        raise ManifestInvalid("repository.full_name must be <owner>/<name>")
    if repo["url"] != f"https://github.com/{repo['full_name']}":
        raise ManifestInvalid("repository.url must be https://github.com/<full_name>")
    if not isinstance(data["branch"], str) or not _BRANCH.fullmatch(data["branch"]) or data["branch"].endswith(".lock"):
        raise ManifestInvalid("branch must be refs/heads/<name>")
    for key in ("commit", "base_commit"):
        if (key == "commit" or data[key] is not None) and not (isinstance(data[key], str) and _HEX40.fullmatch(data[key])):
            raise ManifestInvalid(f"{key} must be 40 lowercase hex")
    if not isinstance(by, dict) or set(by) != _BY_KEYS or not all(isinstance(by[k], str) and by[k] for k in _BY_KEYS):
        raise ManifestInvalid("published_by must be {run_uid, machine}")
    if data["source_kind"] not in SOURCE_KINDS:
        raise ManifestInvalid(f"unknown source_kind {data['source_kind']!r}")
    if data["artifacts"] != []:
        raise ManifestInvalid("artifacts is reserved and must be [] in v1")
    return Manifest(repository_id=repo_id, full_name=repo["full_name"], url=repo["url"], branch=data["branch"],
                    commit=data["commit"], base_commit=data["base_commit"],
                    published_at=_int(data["published_at"], "published_at"),
                    manifest_revision=_int(data["manifest_revision"], "manifest_revision", 1),
                    published_by=dict(by), source_kind=data["source_kind"], raw=data)


@dataclass(frozen=True)
class Custody:
    source_holder: Optional[str]
    state: str
    manifest_revision: Optional[int]
    attached_run_uid: Optional[str] = None
    reason: Optional[str] = None


def parse_custody(value: Any) -> Optional[Custody]:
    """Board-written ``workspace_custody``; anything unreadable counts as ``retained`` (fail closed)."""
    if value is None:
        return None
    try:
        data = _decode(value)
    except (TypeError, ValueError):
        return Custody(None, "retained", None, reason="malformed_custody")
    if data is None:
        return None
    if not isinstance(data, dict) or data.get("state") not in CUSTODY_STATES:
        return Custody(None, "retained", None, reason="malformed_custody")
    rev = data.get("manifest_revision")
    return Custody(source_holder=data.get("source_holder") or None, state=data["state"],
                   manifest_revision=rev if isinstance(rev, int) and not isinstance(rev, bool) else None,
                   attached_run_uid=data.get("attached_run_uid"), reason=data.get("reason"))


# --- the grant snapshot ----------------------------------------------------------------------------------------------

SNAPSHOT_KEYS = ("workspace_source", "workspace_custody")


@dataclass(frozen=True)
class SourceSnapshot:
    """``workspace_source``/``workspace_custody`` as of the grant (same Board transaction), from the lease response
    row. Keys absent (an old Board) is ``None`` from :func:`snapshot_from_row`; present-but-null is a snapshot with
    no source (a new Board, a card without custody)."""
    manifest_value: Any
    custody: Optional[Custody]

    @property
    def has_source(self) -> bool:
        return self.manifest_value is not None or self.custody is not None

    def manifest(self) -> Manifest:
        if self.manifest_value is None:
            raise ManifestInvalid("no manifest")
        return parse_manifest(self.manifest_value)


def snapshot_from_row(row: Any) -> Optional[SourceSnapshot]:
    if not isinstance(row, Mapping) or not all(k in row for k in SNAPSHOT_KEYS):
        return None
    return SourceSnapshot(manifest_value=row["workspace_source"], custody=parse_custody(row["workspace_custody"]))


def replica_snapshot(row: Any) -> Optional[SourceSnapshot]:
    """The replica's (possibly stale) copy of the pull-only columns, for early skips and the fence only: the resolver
    decides from the grant snapshot."""
    keys = row.keys() if hasattr(row, "keys") else ()
    if not all(k in keys for k in SNAPSHOT_KEYS):
        return None
    return SourceSnapshot(manifest_value=row["workspace_source"], custody=parse_custody(row["workspace_custody"]))


# Grant snapshots, handed from the claim (``kanban_db_sync.stage_claim``) to the dispatcher in the same process.
_GRANT_SNAPSHOTS: dict[str, SourceSnapshot] = {}


def stash_grant_snapshot(task_id: str, snapshot: Optional[SourceSnapshot]) -> None:
    if snapshot is None:
        _GRANT_SNAPSHOTS.pop(task_id, None)
    else:
        _GRANT_SNAPSHOTS[task_id] = snapshot


def pop_grant_snapshot(task_id: str) -> Optional[SourceSnapshot]:
    return _GRANT_SNAPSHOTS.pop(task_id, None)


# --- host-local skip memo (v3 §6b) ------------------------------------------------------------------------------

_SKIP_DDL = ("CREATE TABLE IF NOT EXISTS kanban_source_skip (task_id TEXT NOT NULL, manifest_revision INTEGER NOT NULL, "
             "code TEXT NOT NULL, at INTEGER NOT NULL, PRIMARY KEY (task_id, manifest_revision))")


def remember_skip(conn: sqlite3.Connection, task_id: str, manifest_revision: Optional[int], code: str) -> None:
    conn.execute(_SKIP_DDL)
    conn.execute("INSERT OR REPLACE INTO kanban_source_skip (task_id, manifest_revision, code, at) VALUES (?, ?, ?, ?)",
                 (task_id, int(manifest_revision or 0), code, int(time.time())))


def skipped(conn: sqlite3.Connection, task_id: str, manifest_revision: Optional[int]) -> Optional[str]:
    """The host-scope code this host refused ``(task, manifest_revision)`` with, while the memo is fresh. A new
    revision (a fresh publish) clears it implicitly."""
    try:
        row = conn.execute("SELECT code, at FROM kanban_source_skip WHERE task_id = ? AND manifest_revision = ?",
                           (task_id, int(manifest_revision or 0))).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or time.time() - row[1] > SKIP_MEMO_SECONDS:
        return None
    return row[0]


# --- pickup skip (v3 §6) -------------------------------------------------------------------------------------------

def pickup_skip(conn: sqlite3.Connection, card: Any, own: str, *, cfg: Optional[Settings] = None,
                proven_ids: Optional[set[int]] = None) -> Optional[str]:
    """Early skip for a card with custody or a manifest held by another host: it needs a valid replica manifest,
    custody ``published``/``unchanged`` and a locally proven clone for the repository. Only when enabled; the Board
    lease stays the single-writer authority (this never admits anything)."""
    cfg = cfg or settings()
    snap = replica_snapshot(card)
    if not cfg.enabled or snap is None or not snap.has_source:
        return None
    if snap.custody is not None and snap.custody.source_holder == own:
        return None
    try:
        manifest = snap.manifest()
    except ManifestInvalid as exc:
        return f"workspace_source: manifest_invalid ({exc})"
    if snap.custody is None or snap.custody.state not in PORTABLE_STATES:
        return f"workspace_source: custody {snap.custody.state if snap.custody else 'missing'}"
    if (code := skipped(conn, card["id"], manifest.manifest_revision)) is not None:
        return f"workspace_source: refused here ({code}) at revision {manifest.manifest_revision}"
    if proven_ids is None:
        from hermes_cli.kanban_source_clones import proven_repo_ids
        proven_ids = proven_repo_ids(cfg)
    if manifest.repository_id not in proven_ids:
        return f"workspace_source: no proven clone of {manifest.full_name}"
    return None


# --- R4c fence -------------------------------------------------------------------------------------------------------

def fence(conn: sqlite3.Connection, task_id: str, column: str, value: Any) -> None:
    """Refuse a CHANGED ``workspace_kind``/``branch_name`` on a card whose replica carries custody or a manifest
    (the Board fences the same fields; refusing here first keeps the op out of the outbox)."""
    if column not in FENCED_COLUMNS:
        return
    try:
        row = conn.execute(f"SELECT {column}, workspace_source, workspace_custody FROM tasks WHERE id = ?",
                           (task_id,)).fetchone()
    except sqlite3.OperationalError:  # pre-migration file: no pull-only columns, nothing to fence
        return
    if row is None or row[0] == value:
        return
    if row[1] is not None or parse_custody(row[2]) is not None:
        raise SourceFenced(f"{task_id}: {column} is fenced on a card with a workspace source (source_fenced)")


def json_column(value: Any) -> Any:
    """A pulled ``workspace_source``/``workspace_custody`` value as stored locally (JSON TEXT or NULL)."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), sort_keys=True)
