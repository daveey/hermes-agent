"""Real origin refs exercise orphan cleanup, leases and per-item fault isolation."""
import pytest

from scripts.ci import upstream_sync as sync
from tests.scripts.ci.test_upstream_sync import FakeActions, commit, gate, git, make_world, merge_upstream, upstream_commit
from tests.scripts.ci.test_upstream_sync_superseded import (
    assert_closed, branch, host, landed_elsewhere, open_pr, other_merge,
)


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def orphan(root, sha):
    ref = sync.BRANCH_PREFIX + sha
    git(host(root), "push", "-q", "origin", f"{sha}:refs/heads/{ref}")
    return ref


def test_orphan_deleted_without_pr(world, capsys):
    root, checkout, main0, up, actions = landed_elsewhere(world)
    sha = other_merge(root, main0, up, "abandoned staging")
    ref = orphan(root, sha)
    assert gate(checkout, None, actions) == sync.NOT_YET
    assert not branch(root, ref)
    out = capsys.readouterr().out
    assert f"DELETED superseded sync branch refs/heads/{ref} at {sha}" in out
    assert "superseded sync branches: 1 deleted, 0 failed" in out


def test_orphan_missing_parent_kept(world):
    root, checkout, main0, up, actions = landed_elsewhere(world)
    clone = host(root)
    git(clone, "checkout", "-q", "--detach", main0)
    fork = commit(clone, {"fork.txt": "unlanded"}, "fork")
    sha = other_merge(root, fork, up, "missing fork parent")
    ref = orphan(root, sha)
    assert gate(checkout, None, actions) == sync.NOT_YET
    assert branch(root, ref) == sha


@pytest.mark.parametrize("move_candidate", [False, True])
def test_staged_candidate_kept_when_main_moves_before_cleanup(world, move_candidate):
    root, checkout = world
    up = upstream_commit(root, {"u.txt": "u"}, "upstream")
    main0 = git(checkout, "rev-parse", "HEAD")
    head = merge_upstream(checkout)
    actions = FakeActions(root / "origin.git", root)
    request = actions.request

    def landed_before_cleanup(endpoint):
        if "/pulls?" in endpoint:
            twin = other_merge(root, main0, up, "concurrent landed twin")
            git(host(root), "push", "-q", "origin", f"{twin}:refs/heads/main")
            if move_candidate:
                git(host(root), "push", "-q", "-f", "origin", f"{twin}:refs/heads/{sync.BRANCH_PREFIX}{head}")
        return request(endpoint)

    assert sync.advance(checkout, head, gh_repo="example/fork", request=landed_before_cleanup,
                        gh=actions.gh) == sync.NOT_YET
    expected = git(root / "origin.git", "rev-parse", "main") if move_candidate else head
    assert branch(root, sync.BRANCH_PREFIX + head) == expected


def test_orphan_moved_after_listing_lease_refused_and_continues(world, monkeypatch, capsys):
    root, checkout, main0, up, actions = landed_elsewhere(world)
    heads = sorted(other_merge(root, main0, up, name) for name in ("first", "second"))
    refs = [orphan(root, sha) for sha in heads]
    for ref in refs:
        git(checkout, "fetch", "-q", "origin", ref)
    original = sync._run
    raced = []

    def run(repo, *args, **kwargs):
        result = original(repo, *args, **kwargs)
        if args[:2] == ("ls-remote", "--heads"):
            git(host(root), "push", "-q", "-f", "origin", f"{main0}:refs/heads/{refs[0]}")
            raced.append(True)
        return result

    monkeypatch.setattr(sync, "_run", run)
    assert gate(checkout, None, actions) == sync.NOT_YET
    assert raced and branch(root, refs[0]) == main0
    assert not branch(root, refs[1])
    out = capsys.readouterr().out
    assert "branch kept" in out and "stale info" in out
    assert "superseded sync branches: 1 deleted, 1 failed" in out


@pytest.mark.parametrize("failure", ["exception", "nonzero"])
def test_first_pr_comment_failure_does_not_stop_second(world, capsys, failure):
    root, checkout, main0, up, actions = landed_elsewhere(world)
    first, second = [open_pr(root, actions, other_merge(root, main0, up, name)) for name in ("first", "second")]
    gh = actions.gh

    def comment_fails(*args, **kwargs):
        if args[:3] == ("pr", "comment", str(first["number"])):
            if failure == "exception":
                raise OSError("first comment transport failed")
            import subprocess
            return subprocess.CompletedProcess(args, 1, "", "first comment transport failed")
        return gh(*args, **kwargs)

    assert sync.advance(checkout, None, gh_repo="example/fork", request=actions.request,
                        gh=comment_fails) == sync.NOT_YET
    assert first["state"] == "open" and branch(root, first["head"]["ref"])
    assert_closed(root, second)
    assert "first comment transport failed" in capsys.readouterr().out


def test_landing_candidate_kept_after_create_error_and_concurrent_landing(world):
    root, checkout = world
    up = upstream_commit(root, {"u.txt": "u"}, "upstream")
    main0 = git(checkout, "rev-parse", "HEAD")
    head = merge_upstream(checkout)
    actions = FakeActions(root / "origin.git", root)
    assert gate(checkout, head, actions) == sync.NOT_YET
    actions.ci()

    def create_fails(*args, **kwargs):
        assert args[:2] == ("pr", "create")
        twin = other_merge(root, main0, up, "concurrent landed twin")
        git(host(root), "push", "-q", "origin", f"{twin}:refs/heads/main")
        raise OSError("create transport failed")

    assert sync.advance(checkout, None, gh_repo="example/fork", request=actions.request,
                        gh=create_fails) == sync.REFUSED
    assert branch(root, sync.BRANCH_PREFIX + head) == head


def test_open_release_pr_is_not_an_orphan(world):
    root, checkout, main0, up, actions = landed_elsewhere(world)
    pr = open_pr(root, actions, other_merge(root, main0, up, "release sync"), base="release")
    assert gate(checkout, None, actions) == sync.NOT_YET
    assert pr["state"] == "open" and branch(root, pr["head"]["ref"])
