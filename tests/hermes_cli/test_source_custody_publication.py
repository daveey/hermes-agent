"""Inert publication proof. Real temporary Git and a local bare remote; no production signals."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_source_publication as pub
from hermes_cli import kanban_source_refs as refs

pytestmark = pytest.mark.platforms("posix")


def _env() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
        env.pop(key, None)
    env.update(GIT_AUTHOR_NAME="Custody Test", GIT_AUTHOR_EMAIL="custody@example.com",
               GIT_COMMITTER_NAME="Custody Test", GIT_COMMITTER_EMAIL="custody@example.com")
    return env


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True,
                          text=True, env=_env())


def _pair(tmp: Path):
    bare = tmp / "bare.git"
    src = tmp / "src"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True, text=True, env=_env())
    subprocess.run(["git", "init", str(src)], check=True, capture_output=True, text=True, env=_env())
    _git(src, "config", "user.name", "Custody Test")
    _git(src, "config", "user.email", "custody@example.com")
    (src / "kept.txt").write_text("current\n", encoding="utf-8")
    _git(src, "add", "kept.txt")
    _git(src, "commit", "-m", "current")
    _git(src, "remote", "add", "origin", str(bare))
    _git(src, "push", "origin", "HEAD:refs/heads/main")
    return src, bare


def test_publication_fetch_imports_objects_without_tracking_updates(tmp_path):
    src, bare = _pair(tmp_path)
    oid = _git(src, 'rev-parse', 'HEAD').stdout.strip()
    peer = tmp_path / 'peer'
    subprocess.run(['git', 'clone', '--branch', 'main', str(bare), str(peer)],
                   check=True, capture_output=True, env=_env())
    (peer / 'later').write_text('later\n')
    _git(peer, 'add', 'later')
    _git(peer, 'commit', '-m', 'later')
    _git(peer, 'push', 'origin', 'main')
    before = _git(src, 'for-each-ref').stdout
    fetch_head = src / '.git/FETCH_HEAD'
    fetch_head.write_text('kept\n')
    proof = pub.prove(src, oid, allow_local_remote=pub.TEST_LOCAL_REMOTE)
    assert proof['status'] == 'published', proof
    assert _git(src, 'for-each-ref').stdout == before
    assert fetch_head.read_text() == 'kept\n'


def test_default_local_remote_is_not_publication(tmp_path: Path):
    src, _bare = _pair(tmp_path)
    oid = _git(src, "rev-parse", "HEAD").stdout.strip()
    proof = pub.prove(src, oid, allow_local_remote=False)
    assert proof["status"] == "not_published"
    assert proof["reason"] == "local_remote"
    assert proof["release"] is False


def test_opt_in_bare_remote_proves_equality_and_ancestry_not_an_amended_parent(tmp_path: Path):
    src, bare = _pair(tmp_path)
    first = _git(src, "rev-parse", "HEAD").stdout.strip()
    (src / "kept.txt").write_text("amended\n", encoding="utf-8")
    _git(src, "commit", "--amend", "-m", "amended")
    amended = _git(src, "rev-parse", "HEAD").stdout.strip()
    _git(src, "push", "origin", "+HEAD:refs/heads/main")
    assert pub.prove(src, amended, allow_local_remote=pub.TEST_LOCAL_REMOTE)["status"] == "published"
    old = pub.prove(src, first, allow_local_remote=pub.TEST_LOCAL_REMOTE)
    assert old["status"] == "not_published"
    assert old["release"] is False
    listed = _git(bare, "ls-remote", ".").stdout
    assert "refs/hermes-source-custody/" not in listed


def test_stale_tracking_ref_and_graft_are_not_proof(tmp_path: Path):
    src, _bare = _pair(tmp_path)
    first = _git(src, "rev-parse", "HEAD").stdout.strip()
    (src / "kept.txt").write_text("moved\n", encoding="utf-8")
    _git(src, "commit", "-am", "moved")
    moved = _git(src, "rev-parse", "HEAD").stdout.strip()
    _git(src, "update-ref", "refs/remotes/origin/main", moved)
    proof = pub.prove(src, moved, allow_local_remote=pub.TEST_LOCAL_REMOTE)
    assert proof["status"] == "not_published"
    graft = src / ".git" / "info" / "grafts"
    graft.parent.mkdir(exist_ok=True)
    # A graft that names the unpublished child as the advertised tip's parent would
    # make ancestry succeed. Publication must ignore it.
    graft.write_text(f"{first} {moved}\n", encoding="utf-8")
    control = subprocess.run(
        ["git", "-C", str(src), "merge-base", "--is-ancestor", moved, first],
        capture_output=True, text=True, env=_env(), check=False)
    assert control.returncode == 0
    grafted = pub.prove(src, moved, allow_local_remote=pub.TEST_LOCAL_REMOTE)
    assert grafted["status"] == "not_published"


def test_no_remote_is_a_completed_negative_and_timeout_is_unknown(tmp_path: Path, monkeypatch):
    src = tmp_path / "src"
    subprocess.run(["git", "init", str(src)], check=True, capture_output=True, text=True, env=_env())
    (src / "kept.txt").write_text("x\n", encoding="utf-8")
    _git(src, "add", "kept.txt")
    _git(src, "commit", "-m", "x")
    oid = _git(src, "rev-parse", "HEAD").stdout.strip()
    proof = pub.prove(src, oid)
    assert proof["status"] == "not_published"
    assert proof["reason"] == "no_remote"
    assert proof["release"] is False

    def explode(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=0.01)

    monkeypatch.setattr(refs.subprocess, "run", explode)
    unknown = pub.prove(src, oid, allow_local_remote=pub.TEST_LOCAL_REMOTE)
    assert unknown["status"] == "unknown"
    assert unknown["release"] is False


def test_refspec_guard_rejects_custody_mirror_and_broad_specs():
    assert pub.reject_refspec("refs/heads/main:refs/heads/main") is None
    assert pub.reject_refspec("refs/hermes-source-custody/ab:refs/hermes-source-custody/ab")
    assert pub.reject_refspec("+refs/*:refs/*")
    assert pub.reject_publish_argv(["push", "--mirror", "origin"])
    assert pub.reject_publish_argv(["push", "origin", "refs/hermes-source-custody/x"])
    assert pub.reject_publish_argv(["push", "origin", "HEAD:refs/heads/wt/x"]) is None
    assert pub.reject_publish_argv(["-c", "remote.origin.mirror=true", "push", "origin"])


def test_publication_does_not_signal(monkeypatch, tmp_path: Path):
    called = []
    monkeypatch.setattr(os, "kill", lambda *a: called.append(a))
    src, _bare = _pair(tmp_path)
    oid = _git(src, "rev-parse", "HEAD").stdout.strip()
    pub.prove(src, oid, allow_local_remote=pub.TEST_LOCAL_REMOTE)
    assert called == []


def test_abbreviated_glob_configured_mirror_and_boolean_opt_in(tmp_path: Path):
    assert pub.reject_refspec("hermes-source-custody/ab:refs/heads/x") == "custody_namespace"
    assert pub.reject_refspec("refs/*") == "broad"
    assert pub.reject_refspec("refs/h*:refs/h*") == "broad"
    assert pub.reject_refspec("refs/*:refs/remotes/b/*") == "broad"
    assert pub.remote_transport("host:path") == "network"
    assert pub.remote_transport("/tmp/bare.git") == "local"
    src, _bare = _pair(tmp_path)
    oid = _git(src, "rev-parse", "HEAD").stdout.strip()
    assert pub.prove(src, oid, allow_local_remote=True)["reason"] == "local_remote"
    _git(src, "config", "remote.origin.mirror", "true")
    assert pub.reject_configured_push(src) == "mirror"
    _git(src, "config", "--unset", "remote.origin.mirror")
    _git(src, "config", "remote.origin.push",
         "refs/hermes-source-custody/*:refs/hermes-source-custody/*")
    assert pub.reject_configured_push(src) is not None
    _git(src, "config", "--unset", "remote.origin.push")
    _git(src, "config", "remote.origin.mirror", "yes")
    assert pub.reject_configured_push(src) == "mirror"
    assert pub.reject_publish_argv(["-c", "remote.origin.mirror=yes", "push", "origin"]) == "mirror"
    assert pub.reject_publish_argv(["-c", "remote.origin.mirror=1"]) == "unknown"
    assert pub.remote_transport("lnk2/b:git") == "local"
    link_parent = tmp_path / "lnk2"
    link_parent.mkdir()
    link = link_parent / "b:git"
    link.symlink_to(_bare)
    _git(src, "remote", "set-url", "origin", str(link))
    colon_path = pub.prove(src, oid)
    assert colon_path["status"] == "not_published"
    assert colon_path["reason"] == "local_remote"


@pytest.fixture
def push_pair(tmp_path, monkeypatch):
    """Review reproductions use only real Git and disposable local bare remotes."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    src, bare = _pair(tmp_path)
    _git(src, "branch", "-M", "main")
    _git(src, "config", "branch.main.remote", "origin")
    _git(src, "config", "branch.main.merge", "refs/heads/main")
    return src, bare


def _guard_reason(src, argv):
    from hermes_cli.kanban_source_removal import PublishRefused, guard_publish
    try:
        guard_publish(src, argv)
    except PublishRefused as exc:
        return exc.reason
    return None


@pytest.mark.parametrize("option", [["--repo=safe"], ["--repo", "safe"]])
def test_push_repo_option_with_positional_refuses(push_pair, tmp_path, option):
    src, bare = push_pair
    safe = tmp_path / "safe.git"
    _git(src, "init", "--bare", str(safe))
    _git(src, "remote", "add", "safe", str(safe))
    _git(src, "config", "remote.origin.mirror", "true")
    custody = "refs/hermes-source-custody/fixture"
    _git(src, "update-ref", custody, "HEAD")
    argv = ["push", *option, "origin"]
    reason = _guard_reason(src, argv)
    # Counterfactual oracle: bypassing the guard leaks into this LOCAL fixture.
    _git(src, *argv)
    assert custody in _git(bare, "for-each-ref", "--format=%(refname)").stdout.splitlines()
    assert reason == "unknown"


@pytest.mark.parametrize("mode,override,tag", [
    ("upstream", None, False), ("tracking", None, False),
    ("current", "default", False), ("upstream", "merge", False),
    ("upstream", None, True),
])
def test_push_implicit_custody_destination_refuses(push_pair, mode, override, tag):
    src, bare = push_pair
    custody = "refs/hermes-source-custody/fixture"
    _git(src, "config", "push.default", mode)
    if override == "merge":
        # branch.merge is multivalued: -c adds, rather than replaces, its value.
        _git(src, "config", "--unset", "branch.main.merge")
    else:
        _git(src, "config", "branch.main.merge", custody)
    config = []
    if override == "default":
        config = ["-c", "push.default=upstream"]
    elif override == "merge":
        config = ["-c", f"branch.main.merge={custody}"]
    if tag:
        _git(src, "tag", "main")
    argv = [*config, "push"]
    reason = _guard_reason(src, argv)
    _git(src, *argv)
    assert custody in _git(bare, "for-each-ref", "--format=%(refname)").stdout.splitlines()
    assert reason == "custody_namespace"


def test_push_tag_collision_preserves_pushremote_check(push_pair, tmp_path):
    src, _ = push_pair
    mirror = tmp_path / "mirror.git"
    _git(src, "init", "--bare", str(mirror))
    _git(src, "remote", "add", "backup", str(mirror))
    _git(src, "config", "remote.backup.mirror", "true")
    _git(src, "config", "branch.main.pushRemote", "backup")
    _git(src, "tag", "main")
    assert _guard_reason(src, ["push"]) == "mirror"


@pytest.mark.parametrize("mode,argv", [
    (None, ["push", "origin", "HEAD:refs/heads/main"]),
    ("upstream", ["push"]), (None, ["push"]),
    (None, ["push", "--repo=safe"]),
])
def test_push_ordinary_real_remote_controls(push_pair, mode, argv):
    src, bare = push_pair
    if mode:
        _git(src, "config", "push.default", mode)
    _git(src, "remote", "add", "safe", str(bare))
    assert _guard_reason(src, argv) is None
    _git(src, *argv)
    assert _git(bare, "for-each-ref", "--format=%(refname)").stdout.splitlines() == ["refs/heads/main"]


@pytest.mark.parametrize("mode,expected", [("invalid-mode", "unknown"), ("matching", "broad")])
def test_push_default_unknown_and_matching_refuse(push_pair, mode, expected):
    src, _ = push_pair
    _git(src, "config", "push.default", mode)
    assert _guard_reason(src, ["push"]) == expected


@pytest.mark.parametrize("argv", [["push"], ["push", "origin"], ["push", "--repo=safe"]])
def test_push_lexical_implicit_destination_unknown(argv):
    assert pub.reject_publish_argv(argv) == "unknown"


@pytest.mark.parametrize("mode", [None, "simple", "upstream", "tracking"])
def test_push_checks_all_merge_values_and_path_target(push_pair, mode):
    src, bare = push_pair
    if mode:
        _git(src, "config", "push.default", mode)
    _git(src, "config", "--add", "branch.main.merge", "refs/hermes-source-custody/second")
    assert _guard_reason(src, ["push", str(bare)]) == "custody_namespace"


@pytest.mark.parametrize("mode", ["nothing", "current"])
def test_push_non_upstream_modes_allow_custody_merge(push_pair, mode):
    src, _ = push_pair
    _git(src, "config", "branch.main.merge", "refs/hermes-source-custody/unused")
    _git(src, "config", "push.default", mode)
    assert _guard_reason(src, ["push"]) is None


def test_push_detached_head_has_no_branch_check(push_pair):
    src, _ = push_pair
    _git(src, "checkout", "--detach")
    assert _guard_reason(src, ["push", "origin"]) is None


def test_push_non_head_symbolic_ref_unknown(push_pair):
    src, _ = push_pair
    _git(src, "update-ref", "refs/other/fixture", "HEAD")
    _git(src, "symbolic-ref", "HEAD", "refs/other/fixture")
    assert _guard_reason(src, ["push", "origin"]) == "unknown"
