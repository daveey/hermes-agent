"""workspace_source v1 publish (core PR-2; plan v2 §4 steps 1-8, v3 §2, v4 S3/S6): make a task-owned branch on
GitHub match the card's worktree, then record it on the Board (``/leases publish_source``).

Never stages, commits, resets or forces anything: the push is a hook-free fast-forward guarded by
``--force-with-lease`` to the URL re-verified right before it. Every refusal raises :class:`PublishRefused` with a
named code; the caller turns it into a ``retained`` receipt (the ending still proceeds, S6). No secret value is ever
put into a refusal: a secret-scan hit names the commit, path and line only.
"""

from __future__ import annotations

import fnmatch
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_cli import kanban_source_board as board_calls
from hermes_cli import kanban_source_clones as clones
from hermes_cli.kanban_workspace_source import Settings

MAX_SCAN_COMMITS = 500
MAX_SCAN_BYTES = 20 * 1024 * 1024
DENYLIST = (".env*", "*.pem", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", "*.p12", "*.pfx", "*.ppk", "*.key",
            "*.jks", "*.keystore", ".netrc", ".pgpass", "credentials*.json")
# One isolated git environment for the secret scan AND the push: no system/global config, no replace refs, no
# grafts, no attributes file, no hooks. The scan reads raw objects, so no diff driver, colour, textconv or attribute
# can reshape what it sees, and replace refs or grafts cannot hide an object the push sends.
_ISOLATED_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_NO_REPLACE_OBJECTS": "1",
                 "GIT_GRAFT_FILE": "/dev/null", "GIT_ATTR_NOSYSTEM": "1"}
_ISOLATED_CONFIG = ("-c", "core.attributesFile=/dev/null", "-c", "core.hooksPath=/dev/null",
                    "-c", "core.useReplaceRefs=false", "-c", "core.fsmonitor=false")
_KEY_MARKER = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----")
_KEY_MARKER_BYTES = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----")
_PRINTABLE_RUN = re.compile(rb"[\x20-\x7e\t]{8,}")
# Ignored paths that are build/tool caches, never private data (v2 §4 step 2).
IGNORED_ALLOWLIST = ("__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules",
                     ".venv", "venv", ".tox", "dist", "build", "*.egg-info", ".DS_Store", ".coverage", "htmlcov",
                     ".cache", ".next", ".turbo", "target", ".gradle", ".worktrees")
_PROTECTED = re.compile(r"(main|master|release/.*)")


class PublishRefused(Exception):
    def __init__(self, code: str, detail: str = ""):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}" if detail else code)


class _Deadline:
    """The preflight's own budget (S6): every git call is capped by what is left of it."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic):
        self.clock, self.at = clock, clock() + seconds

    def left(self, cap: float = 60.0) -> float:
        remaining = self.at - self.clock()
        if remaining <= 0:
            raise PublishRefused("preflight_timeout", "the source preflight ran out of time")
        return min(remaining, cap)


def _git(deadline: _Deadline, path: str, *args: str, check: bool = True, cap: float = 60.0) -> str:
    try:
        return clones.git(path, *args, timeout=deadline.left(cap), check=check).stdout
    except TimeoutError as exc:
        raise PublishRefused("preflight_timeout", str(exc)) from exc
    except subprocess.CalledProcessError as exc:
        raise PublishRefused("git_failed", f"git {' '.join(args[:3])}: {(exc.stderr or '').strip()[-200:]}") from exc


@dataclass(frozen=True)
class Observation:
    head: Optional[str]
    branch: Optional[str]  # short name of the checked-out branch
    clean: bool
    remote: Optional[str]  # ls-remote origin refs/heads/<branch>


def ls_remote(deadline: _Deadline, path: str, branch: str) -> Optional[str]:
    out = _git(deadline, path, "ls-remote", "origin", f"refs/heads/{branch}", cap=clones.LS_REMOTE_TIMEOUT_S * 3)
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/heads/{branch}":
            return sha
    return None


def observe(deadline: _Deadline, path: str, *, remote: bool = True) -> Observation:
    if not Path(path).is_dir():
        return Observation(None, None, False, None)
    head = _git(deadline, path, "rev-parse", "--verify", "-q", "HEAD", check=False).strip() or None
    branch = _git(deadline, path, "symbolic-ref", "-q", "--short", "HEAD", check=False).strip() or None
    clean = _git(deadline, path, "status", "--porcelain", "--untracked-files=all") == ""
    return Observation(head, branch, clean, ls_remote(deadline, path, branch) if remote and branch else None)


# --- step 1: branch scope ----------------------------------------------------------------------------------------

def check_branch(task_id: str, branch: Optional[str], *, manifest_branch: Optional[str], branch_name: Optional[str],
                 default_branch: Optional[str], remote_exists: bool) -> None:
    if not branch:
        raise PublishRefused("source_branch_changed", "HEAD is detached; it must be on the task branch")
    if branch == default_branch or _PROTECTED.fullmatch(branch):
        raise PublishRefused("source_branch_protected", f"{branch} is a protected branch")
    if manifest_branch is not None:
        if f"refs/heads/{branch}" != manifest_branch or (branch_name and branch_name != branch):
            raise PublishRefused("source_branch_changed",
                                 f"HEAD is on {branch}; the manifest pins {manifest_branch} (branch_name {branch_name})")
        return
    owned = branch == f"wt/{task_id}" or (branch.count("/") == 1 and branch.endswith(f"/{task_id}"))
    if not owned and (branch_name != branch or remote_exists):
        raise PublishRefused("source_branch_foreign", f"{branch} is not task-owned (wt/{task_id} or <slug>/{task_id}, "
                                                      "or the card's own branch_name absent remotely at first publish)")


# --- step 2: local state ----------------------------------------------------------------------------------------

def _allowlisted(rel: str) -> bool:
    parts = rel.rstrip("/").split("/")
    return any(fnmatch.fnmatch(part, pat) for part in parts for pat in IGNORED_ALLOWLIST)


def check_local(deadline: _Deadline, path: str) -> None:
    if _git(deadline, path, "status", "--porcelain", "--untracked-files=all") != "":
        raise PublishRefused("dirty_source", "the worktree has uncommitted or untracked files")
    ignored = [line[3:] for line in _git(deadline, path, "status", "--porcelain", "--ignored=matching",
                                         "--untracked-files=all").splitlines() if line.startswith("!! ")]
    private = [rel for rel in ignored if not _allowlisted(rel)]
    if private:
        raise PublishRefused("ignored_private_files", f"ignored files outside the cache allowlist: {private[:5]}")
    if (Path(path) / ".gitmodules").exists():
        raise PublishRefused("source_submodules_unsupported", ".gitmodules present")
    for rel in _git(deadline, path, "ls-files", "--", ".gitattributes", "*/.gitattributes").splitlines():
        try:
            text = (Path(path) / rel).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if "filter=lfs" in text:
            raise PublishRefused("source_lfs_unsupported", f"{rel} routes files through git-lfs")


def prove_origin(path: str, cfg: Settings, *, lookup: Optional[Callable[[str], dict]] = None,
                 deadline: Optional[_Deadline] = None) -> clones.ProvenClone:
    """The registered clone this worktree belongs to, proven now (§3: GitHub, no rewrites, repository id).

    With ``deadline`` (an ending's preflight) every git call and the GitHub lookup are capped by what is left of
    it, and a proof that fails once the budget is spent is ``preflight_timeout`` rather than an origin fault."""
    budget = deadline.left if deadline is not None else None
    try:
        common = Path(clones.git(path, "rev-parse", "--path-format=absolute", "--git-common-dir",
                                 timeout=budget(30) if budget else 30).stdout.strip())
    except (subprocess.CalledProcessError, TimeoutError) as exc:
        if deadline is not None:
            deadline.left()
        raise PublishRefused("source_clone_unregistered", f"{path} is not a git worktree ({exc})") from exc
    for key, clone_path in sorted(cfg.clones.items()):
        root = Path(clone_path).expanduser()
        if (root / ".git").resolve() != common.resolve():
            continue
        try:
            return clones.prove_clone(key, str(root), lookup=lookup, budget=budget)
        except clones.ProofFailed as exc:
            if deadline is not None:
                deadline.left()  # a call cut short by the spent budget is a timeout, not an origin fault
            code = {"wrong_repo": "source_remote_unsupported"}.get(exc.code, exc.code)
            raise PublishRefused(code, exc.detail) from exc
    owner = common.parent if common.name == ".git" else common
    raise PublishRefused("source_clone_unregistered",
                         f"{path} is not a worktree of a registered clone (its clone is {owner})")


# --- step 4: secret scan ----------------------------------------------------------------------------------------

def _secret_in(text: str, *, pii: bool = True) -> bool:
    """Whether the redactor masks anything in ``text``. ``pii=False`` ignores the PII-style pass (E.164 phone
    numbers): binary bytes form those by chance, while the high-signal patterns (keys, provider tokens, credential
    assignments) stay on. The phone pass runs last over the other passes' output, so when the full result equals
    the phone pass alone applied to the input, no other pattern matched."""
    from agent.redact import _SIGNAL_PHONE_RE, _redact_phone, redact_sensitive_text
    if not text.strip():
        return False
    # force: the publish gate never honours ``security.redact_secrets: false`` (that only governs display).
    masked = redact_sensitive_text(text, force=True)
    if masked == text:
        return False
    return pii or masked != _SIGNAL_PHONE_RE.sub(_redact_phone, text)


def _denylisted(rel: str) -> bool:
    return any(fnmatch.fnmatch(Path(rel).name, pat) for pat in DENYLIST)


def _isolated(deadline: _Deadline, path: str, *args: str, stdin: bytes = b"", cap: float = 60.0,
              stdout_in_error: bool = False) -> bytes:
    """git in the isolated environment, bytes in and out (nothing decoded by git or by us before the scan). A
    failure's detail carries stderr only (scan stdout is object content), plus stdout when asked (push status)."""
    from tools.environments.local import build_subprocess_env
    env = build_subprocess_env(scrub_secrets=False, inherit_profile_home=False,
                               extra={**clones._BATCH_ENV, **_ISOLATED_ENV})
    try:
        out = subprocess.run(["git", "-C", path, *_ISOLATED_CONFIG, *args], input=stdin,
                             capture_output=True, timeout=deadline.left(cap), check=True, env=env)
    except subprocess.TimeoutExpired as exc:
        raise PublishRefused("preflight_timeout", f"git {' '.join(args[:3])} timed out") from exc
    except subprocess.CalledProcessError as exc:
        raw = ((exc.stdout or b"") if stdout_in_error else b"") + (exc.stderr or b"")
        err = raw.decode("utf-8", "replace").strip()[-200:]
        raise PublishRefused("git_failed", f"git {' '.join(args[:3])}: {err}") from exc
    return out.stdout


def _cat_batch(deadline: _Deadline, path: str, shas: list[str], *, check_only: bool) -> list[tuple[str, str, bytes]]:
    """``(sha, type, raw content)`` per object (``check_only``: the size as ASCII bytes), via ``cat-file`` with no
    filters, textconv or replace."""
    if not shas:
        return []
    out = _isolated(deadline, path, "cat-file", "--batch-check" if check_only else "--batch",
                    stdin=("\n".join(shas) + "\n").encode("ascii"), cap=120.0)
    result, pos = [], 0
    for sha in shas:
        nl = out.index(b"\n", pos)
        header = out[pos:nl].decode("ascii", "replace").split()
        pos = nl + 1
        if len(header) != 3 or header[0] != sha:
            raise PublishRefused("git_failed", f"object {sha[:12]} unreadable during the scan")
        size = int(header[2])
        if check_only:
            result.append((sha, header[1], header[2].encode("ascii")))
        else:
            result.append((sha, header[1], out[pos:pos + size]))
            pos += size + 1
    return result


_BOMS = ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
         (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"))


def _decode(blob: bytes) -> Optional[str]:
    """Text for a blob: UTF-8, or UTF-16/32 by BOM or by its NUL byte pattern; None means binary."""
    for bom, codec in _BOMS:
        if blob.startswith(bom):
            try:
                return blob.decode(codec)
            except UnicodeDecodeError:
                return None
    if b"\x00" in blob:
        head = blob[:4096]
        quarter = [head[i::4].count(0) for i in range(4)]
        n = len(head) // 4
        for codec, zero, text in (("utf-32-le", (1, 2, 3), 0), ("utf-32-be", (0, 1, 2), 3)):
            if n and min(quarter[i] for i in zero) * 4 >= n * 3 and quarter[text] * 8 <= n:
                try:
                    return blob[:len(blob) - len(blob) % 4].decode(codec)
                except UnicodeDecodeError:
                    return None
        even, odd = head[0::2].count(0), head[1::2].count(0)
        if max(even, odd) * 4 >= len(head) and min(even, odd) * 8 <= max(even, odd):
            try:
                return blob[:len(blob) - len(blob) % 2].decode("utf-16-le" if odd > even else "utf-16-be")
            except UnicodeDecodeError:
                return None
        return None
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _blob_secret_line(blob: bytes) -> Optional[int]:
    """1-based line of a credential in a blob (0: found, no single line), or None."""
    if _KEY_MARKER_BYTES.search(blob):
        return 0
    text = _decode(blob)
    if text is None:  # binary: the PEM marker above, plus the high-signal patterns over every printable run
        runs = [m.group(0).decode("ascii") for m in _PRINTABLE_RUN.finditer(blob)]
        found = any(_secret_in(r, pii=False) for r in runs) or _secret_in("\n".join(runs), pii=False)
        return 0 if found else None
    if _KEY_MARKER.search(text):
        return 0
    for no, line in enumerate(text.splitlines(), 1):
        if _secret_in(line):
            return no
    return 0 if _secret_in(text) else None  # multi-line credentials


def _tree_entries(data: bytes) -> list[tuple[str, bytes, str]]:
    """``(mode, name, sha)`` per entry of a raw tree object."""
    entries, pos = [], 0
    while pos < len(data):
        sp = data.index(b" ", pos)
        nul = data.index(b"\0", sp)
        entries.append((data[pos:sp].decode("ascii"), data[sp + 1:nul], data[nul + 1:nul + 21].hex()))
        pos = nul + 21
    return entries


def _name_text(name: bytes) -> str:
    return (_decode(name) or name.decode("latin-1")) if name else ""


def secret_scan(deadline: _Deadline, path: str, base: str, head: str) -> None:
    """Scan exactly the objects a push of ``head`` over ``base`` can send (``rev-list --objects head --not base``
    with replace refs and grafts off). Commits, tags and blobs: the entire raw bytes go to the matcher, unparsed.
    Trees: only the entry names (the fixed ``<mode> <name>\\0<20-byte sha>`` format leaves no other place for data,
    and raw sha bytes false-match). ``head`` is the sha that is then pushed, never a ref name re-resolved later."""
    listed = _isolated(deadline, path, "rev-list", "--objects", "--no-object-names", head, "--not", base,
                       cap=120.0).decode("ascii").split()
    kinds = _cat_batch(deadline, path, listed, check_only=True)
    commits = [sha for sha, kind, _ in kinds if kind == "commit"]
    if len(commits) > MAX_SCAN_COMMITS:
        raise PublishRefused("scan_range_too_large", f"{len(commits)} commits > {MAX_SCAN_COMMITS}")
    if sum(int(size) for _, kind, size in kinds if kind != "commit") > MAX_SCAN_BYTES:
        raise PublishRefused("scan_range_too_large", f"new objects exceed {MAX_SCAN_BYTES} bytes")
    new = {sha for sha, _, _ in kinds}
    objects = {sha: (kind, data) for sha, kind, data in _cat_batch(deadline, path, listed, check_only=False)}
    old = set()  # (path, sha) already on the base: a new tree's unchanged entries are not additions
    for entry in _isolated(deadline, path, "ls-tree", "-r", "-t", "-z", "--full-tree", base, cap=120.0).split(b"\0"):
        meta, _, name = entry.partition(b"\t")
        if name:
            old.add((name, meta.split()[2].decode("ascii")))
    where: dict[str, str] = {}
    walked: set[str] = set()

    def walk(tree: str, prefix: bytes) -> None:
        walked.add(tree)
        for mode, name, sha in _tree_entries(objects[tree][1]):
            full = prefix + name
            if (full, sha) in old:
                continue
            shown = _name_text(full)
            if _secret_in(_name_text(name)) or _KEY_MARKER_BYTES.search(name):
                parent = shown.rsplit("/", 1)[0] if "/" in shown else "."
                raise PublishRefused("secret_scan_refused", f"a new path name under {parent} holds a credential")
            if mode == "40000":
                if sha in new:
                    walk(sha, full + b"/")
            elif mode != "160000":
                if _denylisted(shown):
                    raise PublishRefused("secret_scan_refused", f"the push adds denylisted path {shown}")
                where.setdefault(sha, shown)

    for sha in commits:  # the tree line only locates new paths (denylist, labels); content is matched below
        tree = objects[sha][1].split(b"\n", 1)[0].removeprefix(b"tree ").decode("ascii")
        if tree in new:
            walk(tree, b"")
    # No commit, tag or blob format is parsed for the credential match: their ENTIRE raw bytes (a commit with all
    # its headers, signatures and message) go to the matcher (review 5891362210). A tree's content is its entry
    # names, matched in walk() per new entry; its raw bytes interleave binary shas, which false-match (review
    # 5892051198). A new tree no new commit reaches has every entry name matched here.
    for sha, (kind, data) in objects.items():
        if kind == "tree":
            if sha not in walked and any(_secret_in(_name_text(n)) or _KEY_MARKER_BYTES.search(n)
                                         for _, n, _ in _tree_entries(data)):
                raise PublishRefused("secret_scan_refused", f"tree {sha[:12]} has a credential in a path name")
            continue
        if (line := _blob_secret_line(data)) is not None:
            label = where.get(sha, f"blob {sha[:12]}") if kind == "blob" else f"{kind} {sha[:12]}"
            raise PublishRefused("secret_scan_refused", f"{label}{f':{line}' if line else ''} adds a credential")
        if kind == "tag":
            raise PublishRefused("secret_scan_refused", f"tag object {sha[:12]} in the push range")


# --- the publish --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Published:
    manifest: dict
    commit: str
    revision: int
    pushed: bool
    source_identity: Optional[dict] = None
    stale: Optional[Exception] = None  # applied by the Board, then superseded locally (not portable)


def _default_branch(deadline: _Deadline, path: str) -> tuple[Optional[str], Optional[str]]:
    out = _git(deadline, path, "ls-remote", "--symref", "origin", "HEAD", cap=clones.LS_REMOTE_TIMEOUT_S * 3)
    name = sha = None
    for line in out.splitlines():
        if line.startswith("ref: ") and line.endswith("\tHEAD"):
            name = line[5:-5].removeprefix("refs/heads/")
        elif line.endswith("\tHEAD"):
            sha = line.split("\t")[0]
    return name, sha


def _has_commit(deadline: _Deadline, path: str, sha: str) -> bool:
    return clones.git(path, "cat-file", "-e", f"{sha}^{{commit}}", timeout=deadline.left(), check=False).returncode == 0


def _fetch(deadline: _Deadline, path: str, task_id: str, what: str) -> None:
    _git(deadline, path, "fetch", "--no-tags", "origin", f"+{what}:refs/hermes/source/{task_id}", check=False)


def _credential_config(deadline: _Deadline, path: str, url: str) -> tuple[str, ...]:
    """The credential helper the user's own config names for ``url``, re-supplied to the isolated push (auth only:
    a helper answers username/password and never sees or shapes the objects sent)."""
    if not url.startswith("https://"):
        return ()
    helper = _git(deadline, path, "config", "--get-urlmatch", "credential.helper", url, check=False).strip()
    return ("-c", "credential.helper=", "-c", f"credential.helper={helper}") if helper else ()


def _push(deadline: _Deadline, path: str, cfg: Settings, branch: str, head: str, lease: Optional[str]) -> None:
    url = _git(deadline, path, "ls-remote", "--get-url", "origin").strip()
    # The isolated env of the scan (no global/system config, replace refs, grafts or hooks); pushing to the URL
    # (not the remote name) never reads remote.origin.push/pushurl config, and ``head`` is the scanned sha.
    args = ["-c", "push.default=nothing", *_credential_config(deadline, path, url),
            "push", "--no-verify", "--porcelain", f"--force-with-lease=refs/heads/{branch}:{lease or ''}",
            url, f"{head}:refs/heads/{branch}"]
    try:
        _isolated(deadline, path, *args, cap=cfg.push_timeout_s, stdout_in_error=True)
    except PublishRefused as exc:
        if exc.code == "preflight_timeout":
            raise PublishRefused("push_failed:timeout", exc.detail) from exc
        text = exc.detail.lower()
        klass = ("rejected" if "rejected" in text or "stale info" in text else
                 "auth" if any(w in text for w in ("authentication", "permission", "denied", "403", "401")) else
                 "other")
        raise PublishRefused(f"push_failed:{klass}", text.strip()[-200:]) from exc


def source_artifacts(conn, task_id, prior, run_uid, deadline):
    from hermes_cli import kanban_source_artifacts as artifacts
    try:
        return artifacts.portable(conn, task_id, prior, run_uid, bound=deadline)
    except artifacts.ArtifactRefused as exc:
        raise PublishRefused(exc.code, exc.detail) from exc


def selection_token(conn, task_id, run_uid):
    """The explicit selection read BEFORE transport; publication_fence compares against it."""
    from hermes_cli import kanban_source_artifacts as artifacts
    return artifacts.selected(conn, task_id, run_uid)


def publication_fence(conn, task_id, run, token):
    from hermes_cli import kanban_source_artifacts as artifacts
    try:
        artifacts.publication_fence(conn, task_id, run, token)
    except artifacts.ArtifactRefused as exc:
        raise PublishRefused(exc.code, exc.detail) from exc


def _identity_exempt(run) -> bool:
    return run.source_protocol < 2 and (run.manifest or {}).get("version", 1) < 2


def validate_run_identity(run, *, clone=None):
    """V2 observations fail closed; v1 needs no invented historical identity."""
    from hermes_cli import kanban_source_registration as registration
    from hermes_cli.kanban_source_launch import AttachRefused
    if _identity_exempt(run):
        return
    kind = (run.manifest or {}).get("source_kind") or (run.source_identity or {}).get("source_kind", "scratch_external")
    current = registration.identity_from_clone(clone, kind) if clone is not None else None
    try:
        registration.validate_identity(run, current=current)
    except AttachRefused as exc:
        raise PublishRefused(exc.code, exc.detail) from exc


def prove_run_identity(run, cfg: Settings, deadline: _Deadline, *,
                       lookup: Optional[Callable[[str], dict]] = None) -> Optional[clones.ProvenClone]:
    """An ending's v2 identity check: one origin proof under the ending's deadline, handed on to :func:`publish`
    so the ending proves its origin once. v1 runs prove nothing here (``publish`` proves them when it runs)."""
    if _identity_exempt(run):
        return None
    if not run.path:
        validate_run_identity(run)  # refuses: an observation without a path has no identity to prove
    clone = prove_origin(str(run.path), cfg, lookup=lookup, deadline=deadline)
    validate_run_identity(run, clone=clone)
    return clone


def publish(conn: sqlite3.Connection, task: Any, run: board_calls.AttachedRun, cfg: Settings, deadline: _Deadline,
            *, lookup: Optional[Callable[[str], dict]] = None, machine: str,
            clone: Optional[clones.ProvenClone] = None) -> Published:
    """Steps 1-8 for an attached run's worktree. Returns the Board's new manifest; raises :class:`PublishRefused`.
    ``clone`` is this ending's proof from :func:`prove_run_identity`; without it the origin is proven here."""
    path, prior = str(run.path or ""), run.manifest
    seen = observe(deadline, path, remote=False)
    if not seen.head or not seen.branch:
        raise PublishRefused("source_lost", f"{path} is gone or has no branch checked out")
    if clone is None:
        clone = prove_origin(path, cfg, lookup=lookup, deadline=deadline)
    validate_run_identity(run, clone=clone)
    deadline.left()
    default_name, default_sha = _default_branch(deadline, path)
    remote = ls_remote(deadline, path, seen.branch)
    check_branch(task.id, seen.branch, manifest_branch=(prior or {}).get("branch"),
                 branch_name=(getattr(task, "branch_name", None) or None), default_branch=default_name,
                 remote_exists=remote is not None)
    check_local(deadline, path)
    if remote is not None and remote != seen.head:
        if not _has_commit(deadline, path, remote):
            _fetch(deadline, path, task.id, f"refs/heads/{seen.branch}")
        merge_base = clones.git(path, "merge-base", "--is-ancestor", remote, seen.head, timeout=deadline.left(),
                                check=False)
        if merge_base.returncode != 0:
            raise PublishRefused("remote_diverged", f"origin {seen.branch} {remote[:12]} is not an ancestor of HEAD")
    pushed = False
    if remote != seen.head:
        base = remote or default_sha
        if not base:
            raise PublishRefused("scan_base_missing", "origin has neither the branch nor a default branch")
        if not _has_commit(deadline, path, base):
            _fetch(deadline, path, task.id, base if remote is None else f"refs/heads/{seen.branch}")
            if not _has_commit(deadline, path, base):
                raise PublishRefused("scan_base_missing", f"scan base {base[:12]} is not available locally")
        secret_scan(deadline, path, base, seen.head)
        ttl = max(int(cfg.push_timeout_s + 120), 300)
        try:
            left = board_calls.renew_remaining(conn, task.id, run.run_uid, ttl)
        except board_calls.SourceCallRefused as exc:
            raise PublishRefused("publish_lease_lost", exc.detail) from exc
        if left < cfg.push_timeout_s + 60:
            raise PublishRefused("publish_lease_lost", f"only {left:.0f}s left on the lease")
        _push(deadline, path, cfg, seen.branch, seen.head, remote)
        pushed = True
    after = observe(deadline, path)
    if after.remote != seen.head or after.head != seen.head or not after.clean or after.branch != seen.branch:
        raise PublishRefused("publish_reobserve_failed", "the worktree or the remote branch moved during publish")
    expected = int(prior["manifest_revision"]) if prior else int(run.manifest_revision or 0)
    token = selection_token(conn, task.id, run.run_uid)
    artifacts = source_artifacts(conn, task.id, prior, run.run_uid, deadline)
    deadline.left()
    publication_fence(conn, task.id, run, token)
    source_kind = (prior or {}).get("source_kind") or (run.source_identity or {}).get("source_kind", "task_worktree")
    version = max((prior or {}).get("version", 1), 2 if artifacts or source_kind == "scratch_external" else 1)
    if prior and prior.get("commit") == seen.head and prior.get("artifacts", []) == artifacts:
        return Published(prior, seen.head, expected, pushed)
    manifest = {
        "version": version, "repository": {"id": clone.repository_id, "full_name": clone.full_name,
                                     "url": f"https://github.com/{clone.full_name}"},
        "branch": f"refs/heads/{seen.branch}", "commit": seen.head,
        "base_commit": (prior or {}).get("base_commit") or default_sha, "published_at": int(time.time() * 1000),
        "manifest_revision": expected + 1, "published_by": {"run_uid": run.run_uid, "machine": machine},
        "source_kind": source_kind, "artifacts": artifacts,
    }
    try:
        board_calls.publish(conn, task.id, run.run_uid, expected_revision=expected, manifest=manifest)
    except board_calls.SourceCallRefused as exc:
        raise PublishRefused(exc.code if exc.code == "source_revision_conflict" else f"publish_source:{exc.code}",
                             exc.detail) from exc
    except Exception as exc:  # health: allow BLE001 -- an unanswered publish is fail-closed (retained), never retried
        raise PublishRefused("publish_source:unanswered", str(exc)[:200]) from exc
    from hermes_cli.kanban_source_registration import identity_from_clone
    stale = None
    try:
        publication_fence(conn, task.id, run, token)
    except PublishRefused as exc:
        stale = exc  # the Board applied this revision: record it, but never report it portable
    return Published(manifest, seen.head, expected + 1, pushed, identity_from_clone(clone, source_kind), stale)
