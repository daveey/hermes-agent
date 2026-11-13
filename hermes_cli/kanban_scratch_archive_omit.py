"""Leave git objects the remote already holds out of a complete scratch archive. [daveey]

A full clone's ``.git/objects`` is most of a scratch tree, and it was most of
its archive too (57-80 % of the source on worker-3, written to the same disk
the reclaim was freeing). The remote already holds those objects, so
:func:`plan_repo` decides, per standalone repo, what the archive can leave out:

* every regular file under ``.git/objects/`` (packs, loose objects,
  commit-graph, ...) except ``info/alternates``; in their place the payload
  carries one pack, ``objects/<n>.pack``, of every *local* object that the
  recorded remote-tracking tips do not reach (unpushed commits, the index's
  staged blobs, stash, reflog-only and dangling objects alike), checked with
  ``git index-pack`` before use;
* worktree files whose bytes are exactly a HEAD blob those tips reach (clean
  tracked files): the manifest keeps their path, mode, size, mtime, sha256 and
  blob id, and :func:`restore` writes them back from that blob.

Only remotes with a network URL count (a local path is another tree that may
itself be reclaimed), and only ``refs/remotes/<remote>/*`` of a configured
remote that the remote still holds: each tip must be advertised by
``git ls-remote`` now, or be contained by a branch it advertises (checked in a
commits-only probe clone). A deleted or force-pushed-away tip is dropped, so
its objects go into the supplement pack; a failed probe keeps every object in
the payload. A repo with no such ref, a shallow repo, a non-sha1 repo, or an
``objects`` that is not a plain directory keeps its objects in the payload
(nothing omitted). In a partial clone the walk skips blobs, so a local blob
counts as held only when it is in HEAD's nearest pushed trees; a ``.promisor``
pack alone is not proof (the remote may have dropped that branch since).

The omission is recorded in the manifest (payload version 2, which an older
``verify_complete_archive`` refuses rather than misreads) and as
``objects_omitted: "remote"`` plus the restore steps in the repo's meta.json.
Restore needs the remote: :func:`restore` clones it bare, fetches the recorded
tips, moves those packs in, indexes the supplement pack, writes the omitted
worktree files from their blobs and checks every ref resolves.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Optional

OMITTED_REASON = "remote"
_HEX40 = re.compile(rb"[0-9a-f]{40}")
_KEEP_OBJECT_FILES = ("info/alternates", "info/http-alternates")
RESTORE_STEPS = [
    "git clone --bare <remote url> <tmp> (with the repo's partial-clone filter, if any); "
    "git -C <tmp> fetch <remote url> <each recorded remote_refs oid>",
    "extract tree.tar.gz, then move <tmp>/objects/pack/* into <repo>/.git/objects/pack/",
    "git -C <repo> index-pack --stdin < objects/<n>.pack (the payload's local-only objects; "
    "commits.bundle is a subset)",
    "write each manifest 'omitted' file from `git cat-file blob <git_oid>`, checking sha256, "
    "then its mode and mtime",
    "(kanban_scratch_archive_omit.restore does all of this)",
]


class _Refused(Exception):
    """Internal: this repo keeps its objects (the plan is None)."""


def _network_url(url: str) -> bool:
    """A URL another host serves: scheme://host (not file://) or scp-like ``host:path``."""
    if "://" in url:
        return not url.lower().startswith("file://")
    head, sep, _ = url.partition(":")
    return bool(sep) and len(head) > 1 and "/" not in head and not url.startswith(("/", "."))


def _config(repo: Path, env: dict, git, key_regex: str) -> list[tuple[str, str]]:
    out = git(repo, env, "config", "--get-regexp", key_regex, ok=(0, 1)).stdout
    pairs = []
    for line in out.decode("utf-8", "replace").splitlines():
        key, _, value = line.partition(" ")
        pairs.append((key, value))
    return pairs


def _remote_tips(repo: Path, env: dict, git) -> tuple[dict[str, str], dict[str, str]]:
    """({remote name: url} with a network URL, {refs/remotes/<name>/...: oid}) for those remotes."""
    urls: dict[str, str] = {}
    for key, value in _config(repo, env, git, r"^remote\..*\.url$"):
        name = key[len("remote."):-len(".url")]
        urls.setdefault(name, value)
    remotes = {n: u for n, u in urls.items() if _network_url(u)}
    out = git(repo, env, "for-each-ref", "--format=%(objectname) %(refname)", "refs/remotes/").stdout
    tips = {}
    for line in out.decode("utf-8", "replace").splitlines():
        oid, _, ref = line.partition(" ")
        if any(ref.startswith(f"refs/remotes/{n}/") for n in remotes):
            tips[ref] = oid
    used = {n: u for n, u in remotes.items() if any(r.startswith(f"refs/remotes/{n}/") for r in tips)}
    return used, tips


# Tests point a recorded network URL at a local fixture remote ({url: replacement}); empty in production.
_PROBE_URL_MAP: dict[str, str] = {}
_PROBE_TIMEOUT = 120


def _probe_env() -> dict:
    """Env for the remote probe: the user's git config (credential helpers, URL rewrites) but never a prompt.

    The archive's own env forbids every transport and drops credential helpers,
    so a private remote would always look unreachable (the repo then keeps all
    its objects). Prompts, GCM dialogs, hooks, fsmonitor and lazy fetches stay off;
    ssh runs in BatchMode unless ``GIT_SSH_COMMAND`` says otherwise.
    """
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", GIT_NO_LAZY_FETCH="1")
    count = int(env.get("GIT_CONFIG_COUNT") or 0)
    pairs = [("core.hooksPath", os.devnull), ("core.fsmonitor", "false"),
             ("core.sshCommand", "ssh -o BatchMode=yes"),
             *((f"url.{new}.insteadOf", old) for old, new in sorted(_PROBE_URL_MAP.items()))]
    for key, value in pairs:
        env[f"GIT_CONFIG_KEY_{count}"], env[f"GIT_CONFIG_VALUE_{count}"] = key, value
        count += 1
    env["GIT_CONFIG_COUNT"] = str(count)
    return env


def _probe(cwd: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    """Run git in *cwd*: a neutral temp dir or the probe's own clone, never the (untrusted) scratch repo."""
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, env=env,
                              timeout=_PROBE_TIMEOUT, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise _Refused(f"git {args[0]} probe failed: {exc!r}") from exc
    return proc


def _advertised(cwd: Path, env: dict, url: str) -> set[str]:
    """Every oid *url* advertises now (heads, tags, peeled tags, pull refs, ...)."""
    proc = _probe(cwd, env, "ls-remote", url)
    if proc.returncode:
        raise _Refused(f"ls-remote {url} failed: {proc.stderr[:300]!r}")
    return {line.split()[0].decode() for line in proc.stdout.splitlines()
            if line.strip() and _HEX40.fullmatch(line.split()[0])}


def _held_by_ancestry(repo: Path, cwd: Path, env: dict, url: str, stale: dict[str, str]) -> dict[str, str]:
    """The *stale* tips (no longer advertised) that a branch *url* advertises still contains.

    A remote-tracking ref falls behind as soon as the remote branch moves, so
    "not advertised" usually just means "an ancestor of the new tip". To check,
    clone the remote's commits only (``--filter=tree:0``, with *repo* as the
    reference, so only commits new since the last fetch cross the network) into
    a throwaway bare repo and ask which tips a branch there contains. A deleted
    or force-pushed-away tip is contained by none and is dropped.
    """
    probe = cwd / f"probe-{len(list(cwd.iterdir()))}.git"
    proc = _probe(cwd, env, "clone", "--bare", "-q", "--no-tags", "--filter=tree:0",
                  "--reference", str(repo), url, str(probe))
    if proc.returncode:
        raise _Refused(f"commit probe clone of {url} failed: {proc.stderr[:300]!r}")
    # One commit walk from every advertised branch: a --contains query per stale tip made a core
    # clone's archive 122 s instead of 38 s (31 stale of 1141 tips, 1163 remote branches).
    walk = _probe(probe, env, "rev-list", "--branches")
    if walk.returncode:
        raise _Refused(f"commit probe walk of {url} failed: {walk.stderr[:300]!r}")
    contained = {line.strip().decode() for line in walk.stdout.splitlines()}
    return {ref: oid for ref, oid in stale.items() if oid in contained}


def _verified_tips(repo: Path, remotes: dict[str, str], tips: dict[str, str]) -> tuple[dict, dict]:
    """Only the remote-tracking tips the remote still holds (advertised, or contained by an advertised branch).

    A recorded tip is the archive's proof the remote holds everything it
    reaches, so it is checked against the remote itself: a branch deleted (or
    force-pushed away) and gc'd there would otherwise vanish from both the
    supplement pack and the remote. Any probe failure raises (the repo then
    keeps its objects in the payload).
    """
    env = _probe_env()
    held: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="scratch-archive-probe-") as tmp:
        cwd = Path(tmp)
        for name, url in sorted(remotes.items()):
            mine = {r: o for r, o in tips.items() if r.startswith(f"refs/remotes/{name}/")}
            adv = _advertised(cwd, env, url)
            held.update({r: o for r, o in mine.items() if o in adv})
            stale = {r: o for r, o in mine.items() if o not in adv}
            if stale and adv:
                held.update(_held_by_ancestry(repo, cwd, env, url, stale))
    used = {n: u for n, u in remotes.items() if any(r.startswith(f"refs/remotes/{n}/") for r in held)}
    return used, held


def _object_files(objects: Path, rel: str) -> dict[str, list[int]]:
    """{workspace rel: signature} of every regular file the payload leaves out of ``objects/``."""
    from hermes_cli.kanban_scratch_archive import _signature
    found = {}
    for current, dirs, files in os.walk(objects, followlinks=False):
        for name in files + dirs:
            full = Path(current) / name
            st = full.lstat()
            sub = full.relative_to(objects).as_posix()
            if stat.S_ISLNK(st.st_mode):
                raise _Refused(f"symlink in objects: {sub}")
            if stat.S_ISREG(st.st_mode) and sub not in _KEEP_OBJECT_FILES:
                found[f"{rel}/objects/{sub}"] = _signature(st)
    return found


def _local_objects(repo: Path, env: dict, git, objects: Path) -> set:
    """Local object ids (loose and packed, promisor packs included); alternates are not local."""
    local = set()
    for sub in objects.iterdir():
        if len(sub.name) == 2 and sub.is_dir() and not sub.is_symlink():
            names = ((sub.name + n).encode() for n in os.listdir(sub))
            local.update(oid for oid in names if _HEX40.fullmatch(oid))
    pack = objects / "pack"
    for idx in sorted(pack.glob("*.idx")) if pack.is_dir() else ():
        with open(idx, "rb") as fh:
            out = subprocess.run(["git", "-C", str(repo), "show-index"], stdin=fh, capture_output=True,
                                 env=env, timeout=600, check=False)
        if out.returncode:
            raise _Refused(f"show-index failed for {idx.name}")
        ids = {line.split()[1] for line in out.stdout.splitlines() if line.strip()}
        local |= ids
    return local


def _reachable(repo: Path, env: dict, tips: dict[str, str], *, partial: bool) -> set:
    """Every object the recorded tips reach (offline; a partial clone's missing ones are skipped).

    In a partial clone blobs are not walked (``--filter=blob:none``): checking each
    of history's blobs for presence was ~80 % of the walk on a blobless core clone
    (21 s vs 4 s). Its local blobs then count as unique unless they are in HEAD's
    nearest pushed trees (:func:`_pushed_blobs`).
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--objects", "--missing=allow-any",
         *(["--filter=blob:none"] if partial else []), "--stdin"],
        input="".join(f"{oid}\n" for oid in sorted(set(tips.values()))).encode(),
        capture_output=True, env=env, timeout=1800, check=False)
    if proc.returncode:
        raise _Refused(f"rev-list of remote tips failed: {proc.stderr[:300]!r}")
    return {line[:40] for line in proc.stdout.splitlines() if _HEX40.match(line[:40])}


_MAX_BOUNDARY = 16


def _pushed_blobs(repo: Path, env: dict, git, tips: dict[str, str]) -> set:
    """Blob ids in the trees of HEAD's nearest pushed commits (HEAD itself when the tips reach it).

    ``rev-list --boundary HEAD --not <tips>`` walks commits only, so this stays
    cheap however long the history is. More than _MAX_BOUNDARY boundary commits
    are cut (fewer files omitted, never a wrong omission).
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--boundary", "HEAD", "--stdin"],
        input="".join(f"^{oid}\n" for oid in sorted(set(tips.values()))).encode(),
        capture_output=True, env=env, timeout=600, check=False)
    if proc.returncode:
        raise _Refused("rev-list --boundary HEAD failed")
    lines = proc.stdout.split()
    bases = [line[1:] for line in lines if line.startswith(b"-")][:_MAX_BOUNDARY] if lines else [b"HEAD"]
    blobs = set()
    for base in bases:
        out = git(repo, env, "ls-tree", "-r", "-z", "--full-tree", base.decode(), ok=(0, 128))
        for record in out.stdout.split(b"\0") if not out.returncode else ():
            parts = record.partition(b"\t")[0].split()
            if len(parts) == 3 and parts[1] == b"blob":
                blobs.add(parts[2])
    return blobs


def _head_blobs(repo: Path, env: dict, git, prefix: str, reachable: set) -> dict[str, str]:
    """{workspace rel: blob id} for HEAD's regular files whose blob the remote tips reach."""
    proc = git(repo, env, "ls-tree", "-r", "-z", "--full-tree", "HEAD", ok=(0, 128))
    if proc.returncode:
        return {}
    blobs = {}
    for record in proc.stdout.split(b"\0"):
        info, _, path = record.partition(b"\t")
        parts = info.split()
        if len(parts) == 3 and parts[0] in (b"100644", b"100755") and parts[2] in reachable:
            rel = os.fsdecode(path)
            blobs[f"{prefix}{rel}"] = parts[2].decode()
    return blobs


def plan_repo(repo: Path, workspace: Path, env: dict, git, *, partial: bool) -> Optional[dict]:
    """What the payload may leave out of *repo*, or None (keep everything).

    Returns ``{"git_dir", "remotes", "remote_refs", "unique", "files", "blobs"}``:
    *unique* the local-only object ids for the supplement pack, *files* the
    omitted object files' signatures (re-checked against the payload
    snapshot), *blobs* the omittable clean worktree files.
    """
    from hermes_cli.kanban_scratch_archive import ArchiveRefused
    rel = repo.relative_to(workspace).as_posix()
    git_rel = ".git" if rel == "." else f"{rel}/.git"
    objects = repo / ".git" / "objects"
    try:
        fmt = git(repo, env, "config", "--get", "extensions.objectformat", ok=(0, 1)).stdout.strip()
        if fmt not in (b"", b"sha1") or os.path.lexists(repo / ".git" / "shallow"):
            return None
        if not stat.S_ISDIR(objects.lstat().st_mode):
            return None
        remotes, tips = _remote_tips(repo, env, git)
        if not tips:
            return None
        remotes, tips = _verified_tips(repo, remotes, tips)
        if not tips:
            return None
        files = _object_files(objects, git_rel)
        local = _local_objects(repo, env, git, objects)
        reachable = _reachable(repo, env, tips, partial=partial)
        pushed = _pushed_blobs(repo, env, git, tips)
        # Held = reached from a verified tip (commits and trees only, in a partial clone), or a blob
        # of HEAD's nearest pushed trees. A promisor pack alone proves nothing: git's "promised" rule
        # also covers objects of a branch the remote has since deleted and gc'd.
        unique = sorted(local - reachable - pushed)
        blobs = _head_blobs(repo, env, git, "" if rel == "." else f"{rel}/", pushed)
    except (_Refused, ArchiveRefused, OSError, subprocess.SubprocessError):
        return None  # any doubt: keep this repo's objects in the payload
    return {"git_dir": git_rel, "remotes": remotes, "remote_refs": tips,
            "unique": [u.decode() for u in unique], "files": files, "blobs": blobs}


def write_supplement(repo: Path, env: dict, plan: dict, payload: Path, index: int) -> dict:
    """Write the local-only objects as ``objects/<index>.pack`` under *payload*; prove it with index-pack."""
    from hermes_cli.kanban_scratch_archive import ArchiveRefused, _sha256_file
    record = {"git_dir": plan["git_dir"], "reason": OMITTED_REASON, "remotes": plan["remotes"],
              "remote_refs": plan["remote_refs"], "object_count": len(plan["unique"]), "pack": None,
              "omitted_object_files": len(plan["files"])}
    if not plan["unique"]:
        return record
    out_dir = payload / "objects"
    out_dir.mkdir(exist_ok=True)
    pack = out_dir / f"{index}.pack"
    with open(pack, "wb") as fh:
        proc = subprocess.run(["git", "-C", str(repo), "pack-objects", "--stdout", "-q"],
                              input="".join(f"{o}\n" for o in plan["unique"]).encode(),
                              stdout=fh, stderr=subprocess.PIPE, env=env, timeout=1800, check=False)
    if proc.returncode:
        raise ArchiveRefused(f"pack-objects failed in {repo}: {proc.stderr[:300]!r}")
    with tempfile.TemporaryDirectory(dir=payload) as tmp:
        idx = Path(tmp) / "check.idx"
        _run_git(repo, env, "index-pack", "-o", str(idx), str(pack))
        with open(idx, "rb") as fh:
            shown = subprocess.run(["git", "-C", str(repo), "show-index"], stdin=fh, capture_output=True,
                                   env=env, timeout=600, check=False)
    got = sorted(line.split()[1].decode() for line in shown.stdout.splitlines() if line.strip())
    if shown.returncode or got != plan["unique"]:
        raise ArchiveRefused(f"supplement pack for {repo} does not hold exactly the local-only objects")
    record.update(pack=f"objects/{index}.pack", pack_sha256=_sha256_file(pack), pack_size=pack.stat().st_size)
    return record


def _run_git(repo: Path, env: Optional[dict], *args: str) -> bytes:
    from hermes_cli.kanban_scratch_archive import ArchiveRefused
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=env, timeout=1800,
                          check=False, stdin=subprocess.DEVNULL)
    if proc.returncode:
        raise ArchiveRefused(f"git {' '.join(args[:2])} failed in {repo}: "
                             f"{proc.stderr.decode('utf-8', 'replace')[:400]}")
    return proc.stdout


class Omitter:
    """``_scan`` hook: drops object files and clean tracked files from the tarball, recording the latter."""

    def __init__(self, plans: list[dict]):
        self.skip = {rel for p in plans for rel in p["files"]}
        self.blobs = {rel: (i, oid) for i, p in enumerate(plans) for rel, oid in p["blobs"].items()}
        self.omitted: dict[str, dict] = {}

    def __call__(self, dfd: int, name: str, rel: str, st: os.stat_result) -> bool:
        from hermes_cli.kanban_scratch_archive import ArchiveRefused, _signature
        if rel in self.skip:
            return True
        want = self.blobs.get(rel)
        if want is None or st.st_nlink != 1:
            return False
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
        with os.fdopen(fd, "rb") as fh:
            fst = os.fstat(fh.fileno())
            if not stat.S_ISREG(fst.st_mode) or _signature(fst) != _signature(st):
                raise ArchiveRefused(f"{rel} changed during archive")
            blob, digest, size = hashlib.sha1(b"blob %d\0" % fst.st_size), hashlib.sha256(), 0
            for block in iter(lambda: fh.read(1 << 20), b""):
                blob.update(block)
                digest.update(block)
                size += len(block)
        if size != fst.st_size or blob.hexdigest() != want[1]:
            return False  # differs from HEAD: archived as bytes
        self.omitted[rel] = {"type": "file", "mode": stat.S_IMODE(st.st_mode), "size": size,
                             "sha256": digest.hexdigest(), "mtime": int(st.st_mtime),
                             "git_oid": want[1], "repo": want[0]}
        return True


def check_files_unchanged(plans: list[dict], snapshot: dict[str, list[int]]) -> None:
    """Raise unless the payload snapshot's object files are exactly those the plans were built from."""
    from hermes_cli.kanban_scratch_archive import ArchiveRefused
    for plan in plans:
        prefix = f"{plan['git_dir']}/objects/"
        now = {rel: sig for rel, sig in snapshot.items()
               if rel.startswith(prefix) and stat.S_ISREG(sig[0])
               and rel[len(prefix):] not in _KEEP_OBJECT_FILES}
        if now != plan["files"]:
            raise ArchiveRefused(f"{plan['git_dir']} objects changed while the archive was built")


def verify_omissions(base: Path, manifest: dict) -> None:
    """Check the omission records of a version-2 manifest (pack hashes, record shape)."""
    from hermes_cli.kanban_scratch_archive import ArchiveRefused, _sha256_file
    repos = manifest["omitted_repos"]
    for record in repos:
        if record.get("reason") != OMITTED_REASON or not record.get("remote_refs"):
            raise ArchiveRefused("malformed omitted-objects record")
        if record["pack"] is not None:
            pack = base / record["pack"]
            if not re.fullmatch(r"objects/\d+\.pack", record["pack"]) or pack.is_symlink():
                raise ArchiveRefused("malformed supplement pack name")
            if pack.stat().st_size != record["pack_size"] or _sha256_file(pack) != record["pack_sha256"]:
                raise ArchiveRefused(f"supplement pack {record['pack']} hash mismatch")
    for rel, rec in manifest["omitted"].items():
        if (rel in manifest["entries"] or rec.get("type") != "file" or not 0 <= rec["repo"] < len(repos)
                or not re.fullmatch(r"[0-9a-f]{40}", rec["git_oid"])):
            raise ArchiveRefused(f"malformed omitted entry {rel!r}")


# --- restore ---------------------------------------------------------------------------------

def _restore_env(url_map: dict[str, str]) -> dict:
    """Network allowed (restore fetches); *url_map* applies to every git child, lazy fetches included."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    env.pop("GIT_NO_LAZY_FETCH", None)
    count = int(env.get("GIT_CONFIG_COUNT") or 0)
    for old, new in sorted(url_map.items()):
        env[f"GIT_CONFIG_KEY_{count}"], env[f"GIT_CONFIG_VALUE_{count}"] = f"url.{new}.insteadOf", old
        count += 1
    env["GIT_CONFIG_COUNT"] = str(count)
    return env


def _fetch_remote_objects(repo: Path, record: dict, env: dict) -> None:
    """Clone each recorded remote bare into a temp dir, fetch the recorded tips, move its objects into *repo*."""
    for name, url in sorted(record["remotes"].items()):
        oids = sorted({o for r, o in record["remote_refs"].items() if r.startswith(f"refs/remotes/{name}/")})
        filt = _run_git(repo, env, "config", "--default", "", "--get", f"remote.{name}.partialclonefilter")
        filt = filt.decode().strip()
        with tempfile.TemporaryDirectory(dir=repo / ".git") as tmp:
            bare = Path(tmp) / "remote.git"
            args = ["clone", "--bare", "-q", "--no-local", "--no-tags",
                    *([f"--filter={filt}"] if filt else []), url, str(bare)]
            _run_git(repo.parent, env, *args)
            # A recorded tip the clone already holds (an ancestor of a moved branch) needs no fetch.
            have = subprocess.run(["git", "-C", str(bare), "cat-file", "--batch-check"],
                                  input="".join(f"{o}\n" for o in oids).encode(), capture_output=True,
                                  env=dict(env, GIT_NO_LAZY_FETCH="1"), timeout=600, check=False)
            present = {line.split()[0].decode() for line in have.stdout.splitlines()
                       if not have.returncode and not line.endswith(b" missing")}
            oids = [o for o in oids if o not in present]
            if oids:
                _run_git(bare, env, "-c", "fetch.unpackLimit=1", "fetch", "-q", "--no-tags",
                         "--no-write-fetch-head", "origin", *oids)
            _move_objects(bare / "objects", repo / ".git" / "objects")


def _move_objects(src: Path, dest: Path) -> None:
    """Move every pack and loose object of *src* into the object store *dest* (never overwriting)."""
    for sub in src.iterdir():
        if sub.name == "pack" or (len(sub.name) == 2 and sub.is_dir()):
            (dest / sub.name).mkdir(exist_ok=True)
            for item in sub.iterdir():
                if not (dest / sub.name / item.name).exists():
                    shutil.move(str(item), str(dest / sub.name / item.name))


def _write_omitted(target: Path, manifest: dict, env: dict) -> None:
    """Write each omitted file back from its blob.

    Runs after the extraction applied the archived directory modes and mtimes,
    so a read-only directory gets owner write only while its files are written,
    and every touched directory gets its archived mode and mtime back.
    """
    from hermes_cli.kanban_scratch_archive import ArchiveRefused
    repos = manifest["omitted_repos"]
    parents: dict[Path, os.stat_result] = {}
    try:
        for rel, rec in sorted(manifest["omitted"].items()):
            git_dir = repos[rec["repo"]]["git_dir"]
            repo = target / git_dir[: -len("/.git")] if git_dir != ".git" else target
            data = _run_git(repo, env, "cat-file", "blob", rec["git_oid"])
            if len(data) != rec["size"] or hashlib.sha256(data).hexdigest() != rec["sha256"]:
                raise ArchiveRefused(f"restored {rel!r} does not match its recorded hash")
            path = target / rel
            parent = path.parent
            if parent not in parents:
                st = parent.lstat()
                if not stat.S_ISDIR(st.st_mode):
                    raise ArchiveRefused(f"parent of {rel!r} is not a directory")
                parents[parent] = st
                if not st.st_mode & stat.S_IWUSR:
                    os.chmod(parent, stat.S_IMODE(st.st_mode) | stat.S_IWUSR | stat.S_IXUSR)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(path, rec["mode"])
            os.utime(path, (rec["mtime"], rec["mtime"]))
    finally:
        for parent, st in sorted(parents.items(), key=lambda kv: -len(kv[0].parts)):
            os.chmod(parent, stat.S_IMODE(st.st_mode))
            os.utime(parent, ns=(st.st_atime_ns, st.st_mtime_ns))


def restore(archive: Path, target: Path, *, url_map: Optional[dict[str, str]] = None) -> dict:
    """Rebuild the archived workspace at *target* (must not exist); returns the manifest.

    *url_map* rewrites a recorded remote URL (e.g. to a mirror). Needs the
    network for archives with omitted objects; refs are checked to resolve
    and, outside partial clones, ``git fsck --connectivity-only`` must pass.
    """
    from hermes_cli.kanban_scratch_archive import PAYLOAD_DIR, _TREE, verify_complete_archive
    manifest = verify_complete_archive(archive)
    base = Path(archive) / PAYLOAD_DIR
    target = Path(target)
    target.mkdir(parents=True)
    with tarfile.open(base / _TREE, "r:gz") as tar:
        # tar_filter's path rules, but the archived mode (it would clear group/other write bits).
        tar.extractall(target, filter=lambda m, dest: tarfile.tar_filter(m, dest).replace(mode=m.mode, deep=False))
    env = _restore_env(url_map or {})
    for record in manifest.get("omitted_repos", []):
        git_dir = record["git_dir"]
        repo = target / git_dir[: -len("/.git")] if git_dir != ".git" else target
        _fetch_remote_objects(repo, record, env)
        if record["pack"] is not None:
            with open(base / record["pack"], "rb") as fh:
                proc = subprocess.run(["git", "-C", str(repo), "index-pack", "--stdin"], stdin=fh,
                                      capture_output=True, env=env, timeout=1800, check=False)
            if proc.returncode:
                raise RuntimeError(f"index-pack of {record['pack']} failed: {proc.stderr[:300]!r}")
    _write_omitted(target, manifest, env)
    for record in manifest.get("omitted_repos", []):
        git_dir = record["git_dir"]
        repo = target / git_dir[: -len("/.git")] if git_dir != ".git" else target
        _check_refs(repo, env)
    return manifest


def _check_refs(repo: Path, env: dict) -> None:
    from hermes_cli.kanban_scratch_archive import ArchiveRefused
    refs = _run_git(repo, env, "for-each-ref", "--format=%(objectname)").split()
    if not refs:
        return
    proc = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch-check"], input=b"\n".join(refs) + b"\n",
                          capture_output=True, env=dict(env, GIT_NO_LAZY_FETCH="1"), timeout=600, check=False)
    if proc.returncode or b" missing" in proc.stdout:
        raise ArchiveRefused(f"restored refs in {repo} do not all resolve")
    partial = _run_git(repo, env, "config", "--default", "", "--get", "extensions.partialclone").strip()
    if not partial:
        _run_git(repo, env, "fsck", "--connectivity-only", "--no-dangling", "--no-progress")
