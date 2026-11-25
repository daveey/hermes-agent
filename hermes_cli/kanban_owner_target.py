"""Read-only source-host observations, never health attestations or receipts.

The local administrator owns session-repair/target-sources.json under Hermes home.
Selectors cannot provide paths, commands or identity overrides. Queue sources require
kind, queue_path, repository, pr, observation_path, train_path and pause_path. Update
sources require kind, repo_path, repository (origin URL), channel, remote_ref,
target_ref, observation_path, stamp_path and stamp_sha_key
(commit/core_sha/full_sha/serves_sha/sha). The current producer supports main only.
Desktop's installed Contents/Resources/install-stamp.json uses commit. A stamp
must describe the *installed* build, not core-update state.json's prebuild offer.
hermes-sync publishes cache/core-update/target-observation.json around its own
successful origin fetch. A scanner or generic file touch cannot renew that fact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import socket
import stat
import subprocess
import time

from hermes_cli.worker_drain_fence import fenced

_SELECTOR = re.compile(r"owner-target:([A-Za-z0-9_.-]{1,64})/([A-Za-z0-9_.-]{1,64})\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_LABEL = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
MAX_BYTES = 1024 * 1024
MAX_AGE = 1200
GIT_TIMEOUT = 3


class Unavailable(ValueError):
    """The trusted source cannot currently prove a disposition."""


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Unavailable("duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise Unavailable('nonfinite JSON constant')


def _path(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise Unavailable("invalid configured path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise Unavailable("configured path must be absolute")
    return path


def _fresh(info, now):
    if not stat.S_ISREG(info.st_mode) or not 0 <= now - info.st_mtime <= MAX_AGE:
        raise Unavailable("stale, future or nonregular source")


def _json(path, now=None, max_bytes=MAX_BYTES):
    # Nonblocking open also prevents a misconfigured FIFO from hanging this CLI.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise Unavailable("not a bounded regular source")
        if now is not None:
            _fresh(before, now)
        raw = stream.read(max_bytes + 1)
        after = os.fstat(stream.fileno())
        if len(raw) > max_bytes or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise Unavailable("source changed or exceeded bound")
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    if not isinstance(value, dict):
        raise Unavailable("expected JSON object")
    return value


@fenced("session-repair.owner-target.git")
def _git(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0",
               GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
    proc = subprocess.Popen(["git", "--no-pager", "-C", str(repo), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    assert proc.stdout is not None  # stdout=PIPE above is invariant.
    output = bytearray()
    deadline = time.monotonic() + GIT_TIMEOUT
    try:
        with selectors.DefaultSelector() as poll:
            poll.register(proc.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not poll.select(remaining):
                    raise Unavailable("git timeout")
                block = os.read(proc.stdout.fileno(), 1025 - len(output))
                if not block:
                    break
                output.extend(block)
                if len(output) > 1024:
                    raise Unavailable("git output exceeded bound")
        code = proc.wait(timeout=max(0.001, deadline - time.monotonic()))
        return code, output.decode("ascii").strip()
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=max(1.0, GIT_TIMEOUT))
        finally:
            proc.stdout.close()


def _ref(repo, ref):
    if not isinstance(ref, str) or not ref.startswith("refs/") or len(ref) > 256:
        raise Unavailable("explicit git ref required")
    code, _ = _git(repo, "check-ref-format", ref)
    if code:
        raise Unavailable("invalid git ref")
    code, head = _git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
    if code or not _SHA.fullmatch(head):
        raise Unavailable("missing full commit")
    return head


def _ancestry(repo, older, newer):
    code, _ = _git(repo, "merge-base", "--is-ancestor", older, newer)
    if code not in (0, 1):
        raise Unavailable("ancestry unavailable")
    return code == 0


def _queue(config, now):
    required = {"kind", "queue_path", "repository", "pr", "observation_path", "pause_path", "train_path"}
    if (set(config) != required or type(config["pr"]) is not int or config["pr"] <= 0
            or not isinstance(config["repository"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", config["repository"])):
        raise Unavailable("invalid queue config")
    queue = _json(_path(config["queue_path"]), now)
    entries = queue.get("entries")
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise Unavailable("invalid queue entries")
    matches = [e for e in entries if type(e.get("pr")) is int and e["pr"] == config["pr"]]
    # Historical terminal entries may coexist with the one live enqueue generation.
    live = [e for e in matches if e.get("state") == "queued"]
    if len(live) != 1:
        raise Unavailable("missing or ambiguous live queue entry")
    entry = live[0]
    ident = entry.get("id")
    vet = entry.get("vet")
    if not isinstance(ident, str) or not _LABEL.fullmatch(ident) or not isinstance(vet, dict):
        raise Unavailable("missing queue generation or vet")
    head = vet.get("head")
    if not isinstance(head, str) or not _SHA.fullmatch(head) or entry.get("judged_head", head) != head:
        raise Unavailable("missing or inconsistent reviewed head")
    revision = ident + ":" + head
    observation = _json(_path(config["observation_path"]), now, max_bytes=4096)
    if (set(observation) != {"version", "repository", "pr", "enqueue_generation", "head", "observed_at", "state"}
            or type(observation["version"]) is not int or observation["version"] != 1
            or observation["repository"] != config["repository"]
            or type(observation["pr"]) is not int or observation["pr"] != config["pr"]
            or observation["enqueue_generation"] != ident or observation["head"] != head
            or observation["state"] != "current"
            or type(observation["observed_at"]) not in (int, float)
            or not 0 <= now - observation["observed_at"] <= MAX_AGE):
        raise Unavailable("missing, stale or mismatched authenticated PR observation")
    pause = _path(config["pause_path"])
    # lstat distinguishes an absent marker from unreadability or broken symlinks.
    try:
        pause.lstat()
        paused = True
    except FileNotFoundError:
        paused = False
    train = _json(_path(config['train_path']), now)
    skip = train.get('skip') or {}
    if not isinstance(skip, dict):
        raise Unavailable('invalid train skip state')
    skip_until = skip.get(ident, 0)
    if type(skip_until) not in (int, float) or not 0 <= skip_until < float('inf'):
        raise Unavailable('invalid train skip deadline')
    alone = any(isinstance(text,str) and re.search(
        r'validate failed on train candidate [0-9a-f]{7,40} with this PR alone on main', text)
        for text in (entry.get('hold_noted'), entry.get('hold_reason')))
    adverse = (paused or queue.get("paused", False) is not False
               or entry.get("paused", False) is not False
               or entry.get("hold_reason") or entry.get("vet_reason")
               or entry.get('hold_candidate') or alone or skip_until > now
               or vet.get("state") != "ready")
    return revision, "failed" if adverse else "pending"


def _update(config, now):
    required = {"kind", "repo_path", "repository", "channel", "remote_ref", "target_ref",
                "observation_path", "stamp_path", "stamp_sha_key"}
    if (set(config) != required or config["stamp_sha_key"] not in {"commit", "core_sha", "full_sha", "serves_sha", "sha"}
            or config["channel"] != "main" or config["remote_ref"] != "refs/heads/main"
            or config["target_ref"] != "refs/remotes/origin/main"
            or not isinstance(config["repository"], str) or not config["repository"]):
        raise Unavailable("invalid update config")
    repo = _path(config["repo_path"])
    fact = _json(_path(config["observation_path"]), now, max_bytes=16384)
    if (set(fact) != {"version", "host", "repo_path", "repository", "channel", "remote_ref",
                      "target_ref", "head", "observed_at", "state"}
            or type(fact["version"]) is not int or fact["version"] != 1
            or fact["host"] != socket.gethostname() or fact["repo_path"] != str(repo.resolve())
            or any(fact[key] != config[key] for key in ("repository", "channel", "remote_ref", "target_ref"))
            or fact["state"] != "current" or type(fact["observed_at"]) not in (int, float)
            or not 0 <= now - fact["observed_at"] <= MAX_AGE
            or not isinstance(fact["head"], str) or not _SHA.fullmatch(fact["head"])):
        raise Unavailable("missing, stale or mismatched updater fetch observation")
    code, origin = _git(repo, "remote", "get-url", "origin")
    target = _ref(repo, config["target_ref"])
    if code or origin != config["repository"] or target != fact["head"]:
        raise Unavailable("updater target changed since fetch")
    stamp = _json(_path(config["stamp_path"]))
    installed = stamp.get(config["stamp_sha_key"])
    if not isinstance(installed, str) or not _SHA.fullmatch(installed):
        raise Unavailable("missing installed full SHA")
    if _ancestry(repo, target, installed):
        disposition = "satisfied"
    elif _ancestry(repo, installed, target):
        disposition = "pending"
    else:
        disposition = "failed"
    final_code, final_origin = _git(repo, "remote", "get-url", "origin")
    if (final_code or final_origin != origin
            or _json(_path(config["stamp_path"])) != stamp
            or _ref(repo, config["target_ref"]) != target
            or _json(_path(config["observation_path"]), now, max_bytes=16384) != fact):
        raise Unavailable("updater observation changed during read")
    return target, disposition


def observe(selector: str, *, registry_path: Path | None = None, now: float | None = None) -> dict:
    """Observe one registered target; registry_path is a local-admin/test API only."""
    now = time.time() if now is None else now
    result = {"version": 1, "observed_at": now, "selector": selector,
              "target": {"kind": "unavailable", "identity": selector, "revision": ""},
              "disposition": "unavailable"}
    try:
        match = _SELECTOR.fullmatch(selector)
        if not match:
            raise Unavailable("invalid selector")
        if registry_path is None:
            from hermes_constants import get_hermes_home
            registry_path = get_hermes_home() / "session-repair" / "target-sources.json"
        registry = _json(registry_path)
        if (set(registry) != {"version", "authority", "sources"}
                or type(registry["version"]) is not int or registry["version"] != 1
                or registry["authority"] != match[1] or not isinstance(registry["sources"], dict)):
            raise Unavailable("registry authority/schema mismatch")
        config = registry["sources"].get(match[2])
        if not isinstance(config, dict) or config.get("kind") not in {"queue", "update"}:
            raise Unavailable("unknown source")
        # Bind actual host and configured component/channel/paths into the target
        # identity. Reusing a registry key for another scope must invalidate old
        # receipts even when both sources happen to offer the same commit.
        scope = json.dumps([socket.gethostname(), config], sort_keys=True, separators=(',', ':'), allow_nan=False)
        result['target']['identity'] = selector + '@' + hashlib.sha256(scope.encode()).hexdigest()
        result["target"]["kind"] = config["kind"]
        reader = _queue if config["kind"] == "queue" else _update
        revision, disposition = reader(config, now)
        result["target"]["revision"] = revision
        result["disposition"] = disposition
    except (OSError, ValueError, TypeError, RecursionError, subprocess.SubprocessError):
        pass  # No source details/paths or arbitrary exception text cross the boundary.
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["observe"])
    parser.add_argument("--selector", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(observe(args.selector), allow_nan=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
