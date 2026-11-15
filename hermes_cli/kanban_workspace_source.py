"""workspace_source v1: manifest + custody parsing, the grant snapshot, resolver refusals, the host-local skip memo,
the pickup skip and the ``workspace_kind``/``branch_name`` fence.

Spec: daveey/hermes ``docs/designs/workspace-source-v1`` (plan v2 §1/§3, v3 §2/§6/§6b, v4 R4b/R4c/S9, v4.1).
Everything here is gated on ``kanban.workspace_source.enabled`` (default off). Core PR-2 completed the protocol
(``SOURCE_PROTOCOL_COMPLETE``): attach before spawn, publish at every voluntary ending, move/reclaim and the CLI gates.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from hermes_cli.kanban_workspace_source_config import DEFAULTS

_log = logging.getLogger(__name__)

# Core PR-2 (preflight/publish, attach + pre-spawn abort, source_protocol) completed the protocol; ``enabled`` is
# honoured. A core without it must keep this False: a half-built client never asks the Board for custody cards (S9).
SOURCE_PROTOCOL_COMPLETE = True

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
                        "source_disabled", "board_lacks_source", "source_identity_refused",
                        "artifact_unavailable", "artifact_mismatch"})
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
            from hermes_cli.config import load_config_readonly
            config = load_config_readonly() or {}
        except Exception:  # health: allow BLE001 -- a broken config never enables anything (fail closed)
            _log.warning("workspace_source: config unreadable; treating as disabled", exc_info=True)
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

    version: int = 1
    artifacts: tuple[dict, ...] = ()

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


def _parse_artifacts(value: Any, version: int) -> list[dict]:
    """Selected attachment references, never local paths or unverified byte metadata."""
    if not isinstance(value, list) or len(value) > 32 or (version == 1 and value):
        raise ManifestInvalid("invalid artifacts count for manifest version")
    seen = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"attachment_uid", "filename", "sha256"}:
            raise ManifestInvalid("artifact must be {attachment_uid, filename, sha256}")
        uid, filename, digest = entry["attachment_uid"], entry["filename"], entry["sha256"]
        if not isinstance(uid, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", uid) or uid in seen:
            raise ManifestInvalid("invalid or duplicate attachment_uid")
        if not isinstance(filename, str) or not filename or filename in {".", ".."} or any(c in filename for c in "/\\\x00"):
            raise ManifestInvalid("artifact filename must be a basename")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ManifestInvalid("artifact sha256 must be 64 lowercase hex")
        seen.add(uid)
    return sorted((dict(entry) for entry in value), key=lambda entry: entry["attachment_uid"])


def parse_manifest(value: Any) -> Manifest:
    """Strict v1/v2 wire schema, at most 8 KiB UTF-8. V1 reserves empty artifacts;
    v2 references explicitly selected Board attachments. Byte custody is verified by the Board."""
    try:
        size = len(value if isinstance(value, (bytes, bytearray)) else
                   (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))).encode("utf-8"))
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
    if type(data["version"]) is not int or data["version"] not in (1, 2):
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
    if not isinstance(data["source_kind"], str) or data["source_kind"] not in SOURCE_KINDS:
        raise ManifestInvalid(f"unknown source_kind {data['source_kind']!r}")
    if data["version"] == 1 and data["source_kind"] != "task_worktree":
        raise ManifestInvalid("scratch_external requires manifest v2")
    data = {**data, "artifacts": _parse_artifacts(data["artifacts"], data["version"])}
    return Manifest(repository_id=repo_id, full_name=repo["full_name"], url=repo["url"], branch=data["branch"],
                    commit=data["commit"], base_commit=data["base_commit"],
                    published_at=_int(data["published_at"], "published_at"),
                    manifest_revision=_int(data["manifest_revision"], "manifest_revision", 1),
                    published_by=dict(by), source_kind=data["source_kind"], raw=data,
                    version=data["version"], artifacts=tuple(dict(entry) for entry in data["artifacts"]))


@dataclass(frozen=True)
class Custody:
    source_holder: Optional[str]
    state: str
    manifest_revision: Optional[int]
    attached_run_uid: Optional[str] = None
    reason: Optional[str] = None
    source_protocol: int = 1
    retained_run_uid: Optional[str] = None


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
    protocol, predecessor = data.get("source_protocol", 1), data.get("retained_run_uid")
    if (type(protocol) is not int or protocol not in (1, 2)
            or (predecessor is not None and (not isinstance(predecessor, str) or not predecessor))):
        return Custody(None, "retained", None, reason="malformed_custody")
    return Custody(source_holder=data.get("source_holder") or None, state=data["state"],
                   manifest_revision=rev if isinstance(rev, int) and not isinstance(rev, bool) else None,
                   attached_run_uid=data.get("attached_run_uid"), reason=data.get("reason"),
                   source_protocol=protocol, retained_run_uid=predecessor)


# --- the grant snapshot ----------------------------------------------------------------------------------------------

SNAPSHOT_KEYS = ("workspace_source", "workspace_custody")


def source_columns(conn: sqlite3.Connection) -> str:
    """The replica's source columns for a SELECT list. They are added by the sync schema (kanban_db_sync), so a board
    that never synced has neither: no card there has custody, so both read as NULL."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    return ", ".join(k if k in have else f"NULL AS {k}" for k in SNAPSHOT_KEYS)


@dataclass(frozen=True)
class SourceSnapshot:
    """``workspace_source``/``workspace_custody`` as of the grant (same Board transaction), from the lease response
    row. Keys absent (an old Board) is ``None`` from :func:`snapshot_from_row`; present-but-null is a snapshot with
    no source (a new Board, a card without custody)."""
    manifest_value: Any
    custody: Optional[Custody]
    grant: Optional[dict] = None  # exact response envelope; never supplied by replica reads

    @property
    def has_source(self) -> bool:
        return self.manifest_value is not None or self.custody is not None

    def manifest(self) -> Manifest:
        if self.manifest_value is None:
            raise ManifestInvalid("no manifest")
        return parse_manifest(self.manifest_value)


def snapshot_from_row(row: Any, *, grant: Optional[dict] = None) -> Optional[SourceSnapshot]:
    if not isinstance(row, Mapping) or not all(k in row for k in SNAPSHOT_KEYS):
        return None
    from copy import deepcopy
    return SourceSnapshot(manifest_value=deepcopy(row["workspace_source"]),
                          custody=parse_custody(row["workspace_custody"]), grant=deepcopy(grant))


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
    if row is None:
        return None
    lifetime = 60 if row[0] == "artifact_unavailable" else SKIP_MEMO_SECONDS
    if time.time() - row[1] >= lifetime:
        return None
    return row[0]


# --- pickup skip (v3 §6) -------------------------------------------------------------------------------------------

_PICKUP_BATCH: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("source_pickup_batch", default=None)


@contextlib.contextmanager
def pickup_batch():
    """One scan's lazy config/proof snapshot; discarded on exit, never used by the post-grant resolver."""
    token = _PICKUP_BATCH.set({})
    try:
        yield
    finally:
        _PICKUP_BATCH.reset(token)


def pickup_skip(conn: sqlite3.Connection, card: Any, own: str, *, cfg: Optional[Settings] = None,
                proven_ids: Optional[set[int]] = None) -> Optional[str]:
    """Early skip for a card with custody or a manifest held by another host: it needs a valid replica manifest,
    custody ``published``/``unchanged`` and a locally proven clone for the repository. Only when enabled; the Board
    lease stays the single-writer authority (this never admits anything)."""
    batch = _PICKUP_BATCH.get()
    if cfg is None:
        if batch is not None:
            if "cfg" not in batch:
                batch["cfg"] = settings()
            cfg = batch["cfg"]
        else:
            cfg = settings()
    snap = replica_snapshot(card)
    if cfg.enabled and snap is not None and not snap.has_source and _legacy_foreign_worktree(card, own):
        return "workspace_source: a legacy worktree card is adopted only by its creator"
    if not cfg.enabled or snap is None or not snap.has_source:
        return None
    from hermes_cli.kanban_source_board import board_lacks_source
    if board_lacks_source():  # S2: an old Board never gets a source card through the legacy path
        return "workspace_source: the Board lacks the source protocol"
    if snap.custody is not None and snap.custody.source_holder == own:
        # A local holder still needs its selected bytes; don't spin on a failed
        # download merely because its Git worktree is already here.
        code = skipped(conn, card["id"], snap.custody.manifest_revision)
        if code in {"artifact_unavailable", "artifact_mismatch"}:
            return f"workspace_source: refused here ({code}) at revision {snap.custody.manifest_revision}"
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
        key = tuple(sorted(cfg.clones.items()))
        if batch is not None:
            proofs = batch.setdefault("proofs", {})
            if key not in proofs:
                proofs[key] = proven_repo_ids(cfg)
            proven_ids = proofs[key]
        else:
            proven_ids = proven_repo_ids(cfg)
    if manifest.repository_id not in proven_ids:
        return f"workspace_source: no proven clone of {manifest.full_name}"
    return None


# --- R4c fence -------------------------------------------------------------------------------------------------------

def _legacy_foreign_worktree(card: Any, own: str) -> bool:
    """v4 S4: a worktree card with no custody and no manifest whose creator is another gateway (only the creator may
    ``legacy_adopt`` it; its worktree lives there)."""
    keys = card.keys() if hasattr(card, "keys") else ()
    if "workspace_kind" not in keys or "created_on" not in keys:
        return False
    return card["workspace_kind"] == "worktree" and bool(card["created_on"]) and card["created_on"] != own


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
