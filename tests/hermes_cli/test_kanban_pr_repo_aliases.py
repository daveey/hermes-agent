"""A renamed GitHub repo's pull URL and its canonical URL are one PR to the respawn guard.

Production shape (t_01a1cca14b7a62d3): the owner's checkpoint captured
``https://github.com/daveey/hermes/pull/1208`` while its reviewer (``gh`` output)
recorded ``https://github.com/softmax-universe/daveey-hermes/pull/1208``. The guard
must treat them as the same pull, and never collapse different repos by number. [daveey]
"""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_pr_policy as policy
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_dependency_review as review
from tests.hermes_cli import test_kanban_dependency_review as reviews
from tests.hermes_cli.kanban_guard_helpers import worker_comment

ALIAS = "https://github.com/daveey/hermes/pull/1208"
CANON = "https://github.com/softmax-universe/daveey-hermes/pull/1208"
HEAD = reviews.HEAD

board = reviews.board
REAL_CONFIGURED_ALIASES = getattr(policy, "_configured_aliases", None)


@pytest.fixture(autouse=True)
def aliases(monkeypatch):
    """Built-in map only, fresh per test; tests may set extra configured entries."""
    configured: dict = {}
    monkeypatch.setattr(policy, "_alias_cache", None, raising=False)
    monkeypatch.setattr(policy, "_configured_aliases", lambda: configured, raising=False)
    yield configured
    policy._alias_cache = None


@pytest.mark.parametrize("url,key", [
    (ALIAS, CANON),
    (ALIAS.upper().replace("HTTPS://GITHUB.COM", "https://github.com") + "/", CANON),
    ("http://github.com/daveey/hermes/pull/1208", CANON),
    (CANON, CANON),
    (CANON + "/", CANON),
    # Not the renamed repo: unchanged (no prefix/suffix or number-only matching).
    ("https://github.com/daveey/hermes-fork/pull/1208", "https://github.com/daveey/hermes-fork/pull/1208"),
    ("https://github.com/xdaveey/hermes/pull/1208", "https://github.com/xdaveey/hermes/pull/1208"),
    ("https://github.com/softmax-universe/other/pull/1208", "https://github.com/softmax-universe/other/pull/1208"),
    ("https://github.com/daveey/hermes/issues/1208", "https://github.com/daveey/hermes/issues/1208"),
])
def test_norm_pr_canonicalizes_only_known_renamed_repos(url, key):
    assert policy.norm_pr(url) == key


def test_canonical_repo_is_case_insensitive():
    assert policy.canonical_repo("Daveey/Hermes") == "softmax-universe/daveey-hermes"
    assert policy.canonical_repo("Other/Repo") == "other/repo"


def test_configured_aliases_extend_builtins_and_ignore_invalid(aliases):
    aliases.update({"Example/Old": " example/repo ", "bad": "example/repo",
                    "example/x": 3, 4: "a/b", "same/repo": "same/repo"})
    assert policy.norm_pr("https://github.com/EXAMPLE/old/pull/7") == "https://github.com/example/repo/pull/7"
    assert policy.norm_pr(ALIAS) == CANON
    assert policy.repo_aliases() == {"daveey/hermes": "softmax-universe/daveey-hermes",
                                     "example/old": "example/repo"}


def test_alias_map_is_cached_until_ttl(monkeypatch, aliases):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    assert "example/old" not in policy.repo_aliases()
    aliases["example/old"] = "example/repo"
    assert "example/old" not in policy.repo_aliases()  # cached
    clock[0] += policy._ALIAS_TTL_SECONDS - 2
    assert "example/old" not in policy.repo_aliases()  # still cached
    clock[0] += 3
    assert policy.repo_aliases()["example/old"] == "example/repo"


def test_real_config_reader(monkeypatch):
    from hermes_cli import config

    monkeypatch.setattr(policy, "_configured_aliases", REAL_CONFIGURED_ALIASES)

    def broken():
        raise ValueError("secret-bearing parse error")
    monkeypatch.setattr(config, "load_config", broken)
    assert policy._configured_aliases() == {}
    assert policy.norm_pr(ALIAS) == CANON  # built-ins survive an unreadable config
    monkeypatch.setattr(config, "load_config", lambda: {"kanban": {"github_repo_aliases": {"a/old": "a/new"}}})
    assert policy._configured_aliases() == {"a/old": "a/new"}
    for cfg in ({"kanban": {"github_repo_aliases": ["a/old"]}}, {"kanban": []}, {"kanban": ["x"]}, []):
        monkeypatch.setattr(config, "load_config", lambda cfg=cfg: cfg)
        assert policy._configured_aliases() == {}


@pytest.mark.parametrize("ref,expected", [
    (CANON, {CANON}),
    (ALIAS, {CANON}),
    ("daveey/hermes#1208", {CANON}),
    ("softmax-universe/daveey-hermes#1208", {CANON}),
    (1208, {CANON}),
    ("https://github.com/softmax-universe/other/pull/1208", set()),
    ("softmax-universe/other#1208", set()),
])
def test_named_prs_resolves_renamed_repo_refs(ref, expected):
    captured = {policy.norm_pr(ALIAS)}
    assert review._named_prs({"pr": ref}, captured) == expected


def _parked(conn, monkeypatch, published):
    monkeypatch.setattr(reviews, "PR", published)
    return reviews.parked(conn)


@pytest.mark.parametrize("published,reviewed", [(ALIAS, CANON), (CANON, ALIAS)])
@pytest.mark.parametrize("verdict", ["REQUEST_CHANGES", "approve"])
def test_review_of_renamed_repo_pr_resumes_owner(board, monkeypatch, published, reviewed, verdict):
    """The 1208 shape both ways: alias checkpoint + canonical review, and vice versa."""
    task, parents = _parked(board, monkeypatch, published)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
    reviews.finish_reviews(board, parents, metadata={"verdict": verdict, "pr": reviewed, "reviewed_sha": HEAD})
    # A reviewer repeating the PR in the other spelling is not a new publication.
    kb.add_comment(board, task, "reviewer", "Reviewed " + reviewed)
    assert kb.get_task(board, task).status == "ready"
    assert dispatch.check_respawn_guard(board, task) is None
    assert kb.claim_task(board, task) is not None
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("reviewed", [
    "https://github.com/softmax-universe/other/pull/1208",
    "https://github.com/daveey/hermes-fork/pull/1208",
    "https://github.com/softmax-universe/daveey-hermes/pull/1209",
])
def test_review_of_other_repo_same_number_stays_held(board, monkeypatch, reviewed):
    task, parents = _parked(board, monkeypatch, ALIAS)
    reviews.finish_reviews(board, parents, metadata={"verdict": "REQUEST_CHANGES", "pr": reviewed,
                                                     "reviewed_sha": HEAD})
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_new_pr_in_other_repo_after_review_stays_held(board, monkeypatch):
    task, parents = _parked(board, monkeypatch, ALIAS)
    reviews.finish_reviews(board, parents, metadata={"verdict": "REQUEST_CHANGES", "pr": CANON,
                                                     "reviewed_sha": HEAD})
    worker_comment(board, task, "dev", "Opened https://github.com/softmax-universe/other/pull/1208")
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_checkpoint_stores_canonical_keys(board, monkeypatch):
    task, _ = _parked(board, monkeypatch, ALIAS)
    payload = json.loads(board.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='dependency_wait'", (task,)).fetchone()[0])
    assert payload["review_checkpoint"]["prs"] == [CANON]


def test_legacy_alias_checkpoint_matches_canonical_review(board, monkeypatch):
    """Checkpoints written before canonicalization stored the lowercased alias URL."""
    task, parents = _parked(board, monkeypatch, ALIAS)
    event = board.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='dependency_wait'",
                          (task,)).fetchone()
    payload = json.loads(event["payload"])
    payload["review_checkpoint"]["prs"] = [ALIAS]
    board.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), event["id"]))
    board.commit()
    reviews.finish_reviews(board, parents, metadata={"verdict": "REQUEST_CHANGES", "pr": CANON,
                                                     "reviewed_sha": HEAD})
    assert dispatch.check_respawn_guard(board, task) is None


@pytest.mark.parametrize("summary_pr", [ALIAS, CANON])
def test_lost_verdict_naming_renamed_pr_is_a_review(summary_pr):
    assert review.is_review_result({"worker_session_id": "s"}, "Looked at " + summary_pr, [ALIAS])
    assert review.is_review_result({"worker_session_id": "s"}, "Looked at " + summary_pr, [CANON])
    assert not review.is_review_result({"worker_session_id": "s"},
                                       "Looked at https://github.com/softmax-universe/other/pull/1208",
                                       [ALIAS])


def test_foreign_contract_in_renamed_spelling_does_not_hold(board):
    owner = kb.create_task(board, title="owner", assignee="dev", completion_contract=ALIAS)
    board.execute("UPDATE tasks SET completion_contract=? WHERE id=?", (ALIAS.upper() + "/", owner))
    board.commit()
    task = kb.create_task(board, title="fyi", assignee="dev")
    assert policy.holding_urls(board, task, [(CANON, "Working on " + CANON)]) == set()
    other = "https://github.com/softmax-universe/other/pull/1208"
    assert policy.holding_urls(board, task, [(other, "Working on " + other)]) == {other}


def test_pr_state_cache_shares_renamed_and_canonical_spellings(monkeypatch):
    calls = []
    monkeypatch.setattr(prs, "_gh_pr_state", lambda url: calls.append(url) or "MERGED")
    prs.clear_cache()
    try:
        assert prs.pr_state(ALIAS) == "MERGED"
        assert prs.pr_state(CANON) == "MERGED"
        assert prs.all_prs_finished([ALIAS, CANON])
        assert calls == [ALIAS]
    finally:
        prs.clear_cache()


def test_all_prs_finished_reads_each_pull_once(monkeypatch):
    calls = []
    monkeypatch.setattr(prs, "pr_state", lambda url: calls.append(url) or "MERGED")
    assert prs.all_prs_finished([ALIAS, CANON + "/"])
    assert calls == [CANON + "/"]
