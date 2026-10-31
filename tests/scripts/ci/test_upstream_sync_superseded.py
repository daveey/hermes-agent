"""Upstream sync gate: open sync PRs that another landed sync made redundant are closed, nothing else is.

Real bare Git remotes; only GitHub's API is the FakeActions fixture from test_upstream_sync.
"""
import subprocess

import pytest

from scripts.ci import upstream_sync as sync
from tests.scripts.ci.test_upstream_sync import FakeActions, commit, gate, git, main_of, make_world, merge_upstream, \
    staged, upstream_commit


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def host(root):
    """Another host's fork checkout: commits made here are not in the gate's checkout."""
    path = root / "host-b"
    if not path.exists():
        subprocess.run(["git", "clone", "-q", str(root / "origin.git"), str(path)], check=True)
        for k, v in (("user.email", "b@example.com"), ("user.name", "B")):
            git(path, "config", k, v)
        git(path, "remote", "add", "upstream", str(root / "upstream.git"))
    git(path, "fetch", "-q", "upstream")
    git(path, "fetch", "-q", "origin")
    return path


def other_merge(root, base, upstream_sha, msg):
    """A sync merge (base, upstream_sha) made on another host."""
    clone = host(root)
    git(clone, "checkout", "-q", "--detach", base)
    git(clone, "merge", "-q", "--no-ff", "-m", msg, upstream_sha)
    return git(clone, "rev-parse", "HEAD")


def open_pr(root, actions, sha, *, ref=None, repo="example/fork", base="main", branch_at=None):
    """A PR main <- ref (default sync/upstream-<sha>) whose origin branch points at branch_at (default sha)."""
    ref = ref or f"{sync.BRANCH_PREFIX}{sha}"
    git(host(root), "push", "-q", str(root / "origin.git"), f"{branch_at or sha}:refs/heads/{ref}")
    pr = {"number": len(actions.pulls) + 1, "state": "open", "base": {"ref": base}, "comments": [],
          "head": {"ref": ref, "sha": sha, "repo": {"full_name": repo}}}
    actions.pulls.append(pr)
    return pr


def branch(root, ref):
    return subprocess.run(["git", "-C", str(root / "origin.git"), "rev-parse", "-q", "--verify", f"refs/heads/{ref}"],
                          capture_output=True, text=True, check=False).stdout.strip()


def parents(root, sha):
    return git(root / "origin.git", "rev-list", "--parents", "-n1", sha).split()[1:]


def assert_closed(root, pr):
    sha = pr["head"]["sha"]
    [comment] = pr["comments"]
    assert pr["state"] == "closed" and main_of(root) in comment and all(p in comment for p in parents(root, sha))
    assert not branch(root, pr["head"]["ref"])


def test_landing_a_sync_closes_its_superseded_twin_and_keeps_an_unlanded_one(world, capsys):
    root, checkout = world
    up = upstream_commit(root, {"u.txt": "u\n"}, "upstream")
    main0 = main_of(root)
    head = merge_upstream(checkout)
    actions = FakeActions(root / "origin.git", root)
    actions.aggregate = None  # the landing PR's required check has not reported yet
    assert gate(checkout, head, actions) == sync.NOT_YET  # staged
    actions.ci()
    twin = open_pr(root, actions, other_merge(root, main0, up, "host b's merge of the same upstream"))
    assert subprocess.run(["git", "-C", str(checkout), "cat-file", "-e", twin["head"]["sha"]],
                          capture_output=True, check=False).returncode  # the gate must fetch it

    assert gate(checkout, head, actions) == sync.NOT_YET  # landing PR opened, waiting for its check
    assert twin["state"] == "open" and staged(root, twin["head"]["sha"])  # main lacks the upstream parent yet
    assert "none superseded" in capsys.readouterr().out

    actions.aggregate = ("completed", "success")
    assert gate(checkout, head, actions) == sync.LANDED
    out = capsys.readouterr().out
    assert_closed(root, twin)
    assert f"CLOSED superseded sync PR #{twin['number']}" in out

    up2 = upstream_commit(root, {"u2.txt": "u2\n"}, "newer upstream")
    unlanded = open_pr(root, actions, other_merge(root, main_of(root), up2, "sync of the newer upstream"))
    assert gate(checkout, None, actions) == sync.NOT_YET
    assert unlanded["state"] == "open" and unlanded["comments"] == [] and staged(root, unlanded["head"]["sha"])
    assert "none superseded" in capsys.readouterr().out


def landed_elsewhere(world):
    """main took another host's sync of upstream ``up`` directly (no PR, no workflow run)."""
    root, checkout = world
    up = upstream_commit(root, {"u.txt": "u\n"}, "upstream")
    main0 = main_of(root)
    git(host(root), "push", "-q", "origin", f"{other_merge(root, main0, up, 'landed sync')}:refs/heads/main")
    return root, checkout, main0, up, FakeActions(root / "origin.git", root)


def test_bare_call_closes_only_same_repo_sync_merges_whose_parents_main_has(world, capsys):
    root, checkout, main0, up, actions = landed_elsewhere(world)
    merge = {name: other_merge(root, main0, up, name) for name in ("superseded", "fork", "feature", "moved", "base")}
    git(host(root), "checkout", "-q", "--detach", main0)
    single = commit(host(root), {"one.txt": "1\n"}, "single parent")
    superseded = open_pr(root, actions, merge["superseded"])
    kept = [open_pr(root, actions, merge["fork"], repo="someone/fork"),
            open_pr(root, actions, merge["feature"], ref="feature/x"),
            open_pr(root, actions, merge["moved"], branch_at=single),  # the branch no longer holds the PR head
            open_pr(root, actions, merge["base"], base="release"),
            open_pr(root, actions, single)]

    assert gate(checkout, None, actions) == sync.NOT_YET  # no candidates, no head: cleanup still runs
    assert_closed(root, superseded)
    assert f"CLOSED superseded sync PR #{superseded['number']}" in capsys.readouterr().out
    for pr in kept:
        assert pr["state"] == "open" and pr["comments"] == [], pr
        assert branch(root, pr["head"]["ref"])
    assert branch(root, f"{sync.BRANCH_PREFIX}{merge['moved']}") == single  # not deleted

    assert gate(checkout, None, actions) == sync.NOT_YET
    assert "none superseded" in capsys.readouterr().out


def test_github_failures_are_logged_and_the_gate_continues(world, capsys):
    root, checkout, main0, up, actions = landed_elsewhere(world)
    first, second = (open_pr(root, actions, other_merge(root, main0, up, name)) for name in ("first", "second"))

    def no_pull_list(endpoint):
        if "/pulls?" in endpoint:
            raise sync.GateError("API down for pulls")
        return actions.request(endpoint)
    upstream_commit(root, {"u2.txt": "u2\n"}, "newer upstream")
    head = merge_upstream(checkout)
    assert sync.advance(checkout, head, gh_repo="example/fork", request=no_pull_list, gh=actions.gh) == sync.NOT_YET
    out = capsys.readouterr().out
    assert staged(root, head) and "API down for pulls" in out  # staged anyway; the listing fault is logged

    actions.gh_fail = {"close": "HTTP 502: Bad Gateway"}
    assert gate(checkout, None, actions) == sync.NOT_YET
    out = capsys.readouterr().out
    assert out.count("HTTP 502: Bad Gateway") == 2  # both PRs tried: one failure does not stop the next
    assert "cleanup incomplete (0 closed, 2 failed)" in out and "none superseded" not in out
    for pr in (first, second):
        assert pr["state"] == "open" and branch(root, pr["head"]["ref"])  # never delete an open PR's branch

    actions.gh_fail = {}
    moved_to = main0

    def push_during_close(pr):  # someone pushes the branch between the identity check and the deletion
        git(host(root), "push", "-q", "-f", "origin", f"{moved_to}:refs/heads/{pr['head']['ref']}")
    actions.on_close = push_during_close
    assert gate(checkout, None, actions) == sync.NOT_YET
    out = capsys.readouterr().out
    assert first["state"] == second["state"] == "closed"
    assert branch(root, first["head"]["ref"]) == branch(root, second["head"]["ref"]) == moved_to
    assert "branch kept" in out
