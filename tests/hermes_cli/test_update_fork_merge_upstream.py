"""Functional test: fork with own commits + updates.fork_merge_upstream merges upstream via ff of a synthesized merge."""
import subprocess
from pathlib import Path
from unittest import mock
import hermes_cli.update_cmd_git as g

def sh(*a, cwd=None):
    return subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

def mkrepo(tmp):
    up = tmp / "up"; up.mkdir(); sh("git", "init", "-q", "-b", "main", cwd=up)
    for k, v in {"user.email": "t@t", "user.name": "t"}.items(): sh("git", "config", k, v, cwd=up)
    (up / "a.txt").write_text("a\n"); (up / "b.txt").write_text("b\n")
    sh("git", "add", "-A", cwd=up); sh("git", "commit", "-qm", "base", cwd=up)
    fork = tmp / "fork.git"; sh("git", "clone", "-q", "--bare", str(up), str(fork))
    live = tmp / "live"; sh("git", "clone", "-q", str(fork), str(live))
    for k, v in {"user.email": "t@t", "user.name": "t"}.items(): sh("git", "config", k, v, cwd=live)
    sh("git", "remote", "add", "upstream", str(up), cwd=live)
    (live / "a.txt").write_text("a fork\n"); sh("git", "commit", "-qam", "fork change", cwd=live); sh("git", "push", "-q", "origin", "main", cwd=live)
    return up, fork, live

def run(live, merge_flag, upstream_edit):
    up = live.parent / "up"
    (up / upstream_edit[0]).write_text(upstream_edit[1]); sh("git", "commit", "-qam", "upstream change", cwd=up)
    with mock.patch.object(g, "_fork_merges_upstream", return_value=merge_flag), \
         mock.patch("hermes_cli.update_cmd_commit.arm_commit_point", return_value=None), \
         mock.patch("hermes_cli.update_cmd_commit.target_syntax_error", return_value=None), \
         mock.patch("hermes_cli.update_cmd._should_skip_upstream_prompt", return_value=False):
        ok = g._sync_with_upstream_if_needed(["git"], live, assume_yes=True)
        if ok: g._push_synced_fork(["git"], live)
    return ok



def test_flag_off_leaves_fork_untouched(tmp_path):
    _up, _fork, live = mkrepo(tmp_path); pre = sh("git", "rev-parse", "HEAD", cwd=live)
    assert run(live, False, ("b.txt", "b up\n"))
    assert sh("git", "rev-parse", "HEAD", cwd=live) == pre


def test_flag_on_merges_upstream_and_pushes(tmp_path):
    _up, fork, live = mkrepo(tmp_path)
    assert run(live, True, ("b.txt", "b up\n"))
    head = sh("git", "rev-parse", "HEAD", cwd=live)
    assert (live / "a.txt").read_text() == "a fork\n" and (live / "b.txt").read_text() == "b up\n"
    assert len(sh("git", "rev-list", "--parents", "-n1", "HEAD", cwd=live).split()) == 3
    assert sh("git", "rev-parse", "main", cwd=fork) == head
    assert sh("git", "status", "--porcelain", cwd=live) == ""


def test_conflict_moves_nothing(tmp_path):
    _up, fork, live = mkrepo(tmp_path); pre = sh("git", "rev-parse", "HEAD", cwd=live)
    assert not run(live, True, ("a.txt", "a upstream conflict\n"))
    assert sh("git", "rev-parse", "HEAD", cwd=live) == pre
    assert sh("git", "rev-parse", "main", cwd=fork) == pre
    assert sh("git", "status", "--porcelain", cwd=live) == ""
