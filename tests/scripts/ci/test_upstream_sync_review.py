"""Upstream sync gate: a sync that carries fork-authored content lands only after an independent exact-head review.

Mechanical sync: every fork merge has an empty ``git show --remerge-diff`` and nothing else on top of upstream.
Fork-authored: a conflict resolution (non-empty remerge-diff) or any non-upstream commit (a ``[daveey]`` fixup).
The review gate is ship's own (hermes-core-wt-land: keyed_review_card, request_review, review_gate); here a stub
module with the same entry points stands in for it, loaded through the same HERMES_CORE_WT_LAND path the gate uses.
Real bare Git remotes; GitHub is the FakeActions fixture from test_upstream_sync.
"""
import json
import subprocess

import pytest

from scripts.ci import upstream_sync as sync
from scripts.ci import upstream_sync_review as review
from tests.scripts.ci.test_upstream_sync import FakeActions, commit, gate, git, landed_by_merge, main_of, make_world, \
    merge_upstream, upstream_commit

STUB_LAND = '''
import json, os

class ReviewRefused(Exception):
    pass

class ReviewCIStopped(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

class Gh:
    def __init__(self, cwd):
        self.cwd = cwd

def _load():
    with open(os.environ["STUB_LAND_STATE"]) as fh:
        return json.load(fh)

def _save(state):
    with open(os.environ["STUB_LAND_STATE"], "w") as fh:
        json.dump(state, fh)

def board_db_paths():
    from pathlib import Path
    return [Path(os.environ["STUB_LAND_STATE"]).with_suffix(".db")]

def keyed_review_card(slug, number, sha):
    return _load()["cards"].get(f"{slug}#{number}:{sha}", "")

def request_review(wt, slug, number, sha, old, base, prior_review, impl_task="", *, gh=None, **kw):
    state = _load()
    state["filed"].append([slug, number, sha, old, base])
    if state.get("worker"):
        _save(state)
        return "", "this Kanban worker shell may not write the board", {"action": "kanban_create",
                                                                        "title": f"review #{number} @{sha[:10]}"}
    card = "t_" + sha[:16]
    state["cards"][f"{slug}#{number}:{sha}"] = card
    _save(state)
    return card, "", None

def review_gate(gh, slug, pr, sha, branch, base, review_task, impl_task=""):
    state = _load()
    state["gates"].append([review_task, pr["number"], sha, branch, base, impl_task])
    _save(state)
    if review_task and state["approve"].get(review_task) == sha:
        return {"source": "kanban", "id": review_task, "reviewed_sha": sha, "verdict": "approve",
                "non_blocking": state.get("nits", [])}
    raise ReviewRefused(f"no independent review approves PR #{pr['number']}'s head {sha[:12]}")

def review_reply_guard(gh, slug, pr, review, reply):
    if review.get("non_blocking") and not reply:
        return 1, "unanswered non-blocking review point(s)"
    return None, ""
'''


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


@pytest.fixture
def land_stub(tmp_path, monkeypatch):
    """ship's review entry points (a stub of hermes-core-wt-land), found through HERMES_CORE_WT_LAND."""
    path = tmp_path / "hermes-core-wt-land"  # no .py suffix, like the real tool
    path.write_text(STUB_LAND)
    state = tmp_path / "land-state.json"
    state.write_text(json.dumps({"cards": {}, "approve": {}, "filed": [], "gates": []}))
    monkeypatch.setenv("HERMES_CORE_WT_LAND", str(path))
    monkeypatch.setenv("STUB_LAND_STATE", str(state))

    class Stub:
        def read(self):
            return json.loads(state.read_text())

        def update(self, **kw):
            data = self.read()
            for key, value in kw.items():
                data[key] = {**data[key], **value} if isinstance(value, dict) else value
            state.write_text(json.dumps(data))

    return Stub()


def fork_main_commit(root, files, msg):
    """A fork-only commit pushed straight to origin's main (the fork's own work)."""
    other = root / "fork-dev"
    if not other.exists():
        subprocess.run(["git", "clone", "-q", str(root / "origin.git"), str(other)], check=True)
        git(other, "config", "user.email", "f@example.com")
        git(other, "config", "user.name", "F")
    git(other, "pull", "-q")
    sha = commit(other, files, msg)
    git(other, "push", "-q", "origin", "main")
    return sha


def conflict_sync(root, checkout):
    """A sync whose merge resolves a conflict by hand: its remerge-diff is non-empty (fork-authored)."""
    fork_main_commit(root, {"shared.txt": "fork\n"}, "fork change")
    upstream_commit(root, {"shared.txt": "upstream\n"}, "upstream change")
    git(checkout, "fetch", "-q", "upstream")
    git(checkout, "fetch", "-q", "origin")
    git(checkout, "checkout", "-q", "--detach", "origin/main")
    merged = subprocess.run(["git", "-C", str(checkout), "merge", "-q", "--no-edit", "upstream/main"],
                            capture_output=True, text=True, check=False)
    assert merged.returncode != 0  # a real conflict
    (checkout / "shared.txt").write_text("fork and upstream\n")
    git(checkout, "add", "shared.txt")
    git(checkout, "commit", "-q", "--no-edit")
    return git(checkout, "rev-parse", "HEAD")


def fixup_sync(root, checkout):
    """A clean sync merge plus a [daveey] fixup commit on top: no conflict, still fork-authored."""
    upstream_commit(root, {"u.txt": "u\n"}, "upstream change")
    merge_upstream(checkout)
    return commit(checkout, {"fixup.txt": "follow upstream\n"}, "[daveey] Upstream sync: fork tests follow upstream")


def staged_green(root, checkout, head):
    actions = FakeActions(root / "origin.git", root)
    assert gate(checkout, head, actions) == sync.NOT_YET  # staged
    actions.ci()
    assert actions.runs[-1]["conclusion"] == "success"
    return actions


# --- RED on main before this gate: the resolution landed with no review ---------------------------------------

def test_conflict_resolution_sync_parks_for_review_then_lands_on_exact_head_approval(world, land_stub, capsys):
    root, checkout = world
    head = conflict_sync(root, checkout)
    before = main_of(root)
    actions = staged_green(root, checkout, head)
    capsys.readouterr()

    assert gate(checkout, head, actions) == sync.NOT_YET  # parks: green CI is not enough
    out = capsys.readouterr().out
    assert main_of(root) == before
    card = "t_" + head[:16]
    assert card in out and "fork-authored" in out and "shared.txt" in out, out
    pr = actions.pulls[0]
    assert land_stub.read()["filed"] == [["example/fork", pr["number"], head, "", "main"]]
    assert land_stub.read()["gates"][0][:5] == ["", pr["number"], head, f"sync/upstream-{head}", "main"]

    land_stub.update(approve={card: "0" * 40})  # an approval of another sha approves nothing
    assert gate(checkout, head, actions) == sync.NOT_YET
    assert main_of(root) == before
    assert len(land_stub.read()["filed"]) == 1  # the keyed card is found again, never filed twice

    land_stub.update(approve={card: head})
    assert gate(checkout, head, actions) == sync.LANDED
    assert landed_by_merge(root, head, before)
    assert land_stub.read()["gates"][-1][:4] == [card, pr["number"], head, f"sync/upstream-{head}"]


# --- Controls ---------------------------------------------------------------------------------------------------

def test_mechanical_sync_lands_without_any_review(world, land_stub):
    root, checkout = world
    upstream_commit(root, {"u.txt": "u\n"}, "upstream change")
    head = merge_upstream(checkout)
    before = main_of(root)
    actions = staged_green(root, checkout, head)
    assert gate(checkout, head, actions) == sync.LANDED
    assert landed_by_merge(root, head, before)
    assert land_stub.read()["filed"] == [] and land_stub.read()["gates"] == []  # ship's gate never consulted


def test_fixup_commit_without_conflict_parks_for_review(world, land_stub, capsys):
    root, checkout = world
    head = fixup_sync(root, checkout)
    before = main_of(root)
    actions = staged_green(root, checkout, head)
    capsys.readouterr()
    assert gate(checkout, head, actions) == sync.NOT_YET
    out = capsys.readouterr().out
    assert main_of(root) == before
    assert head[:10] in out and "[daveey] Upstream sync" in out, out
    land_stub.update(approve={"t_" + head[:16]: head})
    assert gate(checkout, head, actions) == sync.LANDED
    assert landed_by_merge(root, head, before)


def test_unreadable_review_gate_refuses_a_fork_authored_sync(world, land_stub, monkeypatch, tmp_path, capsys):
    root, checkout = world
    head = conflict_sync(root, checkout)
    before = main_of(root)
    actions = staged_green(root, checkout, head)
    monkeypatch.setenv("HERMES_CORE_WT_LAND", str(tmp_path / "absent"))
    assert gate(checkout, head, actions) == sync.REFUSED
    assert main_of(root) == before
    assert "review gate unavailable" in capsys.readouterr().out


def test_worker_shell_gets_the_exact_review_card_action(world, land_stub, capsys):
    root, checkout = world
    head = conflict_sync(root, checkout)
    actions = staged_green(root, checkout, head)
    land_stub.update(worker=True)
    capsys.readouterr()
    assert gate(checkout, head, actions) == sync.NOT_YET
    out = capsys.readouterr().out
    assert '"action": "kanban_create"' in out and "may not write the board" in out, out


def test_unanswered_review_points_park_until_answered(world, land_stub, capsys):
    root, checkout = world
    head = conflict_sync(root, checkout)
    before = main_of(root)
    actions = staged_green(root, checkout, head)
    gate(checkout, head, actions)
    land_stub.update(approve={"t_" + head[:16]: head}, nits=["Nit (non-blocking): wording"])
    capsys.readouterr()
    assert gate(checkout, head, actions) == sync.NOT_YET
    assert "unanswered non-blocking" in capsys.readouterr().out
    assert main_of(root) == before
    assert gate(checkout, head, actions, review=review.ShipReview(checkout, reply="declined: cosmetic")) == sync.LANDED


# --- The classifier itself (each mutant fails a named test) -----------------------------------------------------

def test_classify_reports_the_resolution_files_of_a_conflict_merge(world):
    root, checkout = world
    head = conflict_sync(root, checkout)
    main = git(checkout, "rev-parse", "origin/main")
    found = review.classify(checkout, head, main)
    assert found.fork_authored
    assert found.resolutions == [(head, ["shared.txt"])]
    assert found.commits == []


def test_classify_reports_a_non_upstream_commit(world):
    root, checkout = world
    head = fixup_sync(root, checkout)
    found = review.classify(checkout, head, git(checkout, "rev-parse", "origin/main"))
    assert found.fork_authored
    assert found.resolutions == []
    assert [sha for sha, _ in found.commits] == [head]


def test_octopus_sync_is_fork_authored_and_parks_without_review(world, land_stub, capsys):
    root, checkout = world
    base = main_of(root)
    fork_main_commit(root, {"fork.txt": "fork\n"}, "fork change")
    left = upstream_commit(root, {"left.txt": "left\n"}, "upstream left")
    up = root / "up-work"
    git(up, "checkout", "-q", "-b", "right", base)
    right = commit(up, {"right.txt": "right\n"}, "upstream right")
    git(up, "checkout", "-q", "main")
    git(up, "merge", "-q", "--no-edit", "right")
    git(up, "push", "-q", "origin", "main")
    git(checkout, "fetch", "-q", "upstream")
    git(checkout, "fetch", "-q", "origin")
    git(checkout, "checkout", "-q", "--detach", "origin/main")
    git(checkout, "merge", "-q", "--no-edit", left, right)
    head = git(checkout, "rev-parse", "HEAD")
    before = main_of(root)
    assert git(checkout, "show", "-s", "--format=%P", head).split() == [before, left, right]

    found = review.classify(checkout, head, before)
    # All non-merge commits belong to base or upstream: only the octopus can require review.
    assert found.commits == []
    actions = staged_green(root, checkout, head)
    capsys.readouterr()
    assert gate(checkout, head, actions) == sync.NOT_YET
    assert main_of(root) == before
    assert found.fork_authored
    assert found.resolutions == [(head, ["(octopus merge: no remerge-diff)"])]
    assert found.clean_merges == []
    assert "fork-authored" in capsys.readouterr().out
    assert land_stub.read()["filed"] == [["example/fork", actions.pulls[0]["number"], head, "", "main"]]


def test_nonempty_unparsed_remerge_diff_requires_review(monkeypatch, tmp_path):
    calls = []

    def unparsed_output(repo, *args):
        calls.append((repo, args))
        return "unrecognized non-empty remerge output\n"

    monkeypatch.setattr(review, "_out", unparsed_output)
    assert review.remerge_files(tmp_path, "a" * 40) == ["(unparsed remerge-diff)"]
    assert calls == [(tmp_path, ("show", "--remerge-diff", "--no-color", "--no-ext-diff", "--format=", "a" * 40))]


def test_classify_a_clean_sync_is_mechanical(world):
    root, checkout = world
    upstream_commit(root, {"u.txt": "u\n"}, "upstream change")
    head = merge_upstream(checkout)
    found = review.classify(checkout, head, git(checkout, "rev-parse", "origin/main"))
    assert not found.fork_authored
    assert found.resolutions == [] and found.commits == [] and found.clean_merges == [head]


def test_classify_dry_run_on_landed_heads(world, land_stub, capsys):
    """The CLI dry run judges a landed sync against main just before it landed."""
    root, checkout = world
    head = conflict_sync(root, checkout)
    actions = staged_green(root, checkout, head)
    gate(checkout, head, actions)
    land_stub.update(approve={"t_" + head[:16]: head})
    assert gate(checkout, head, actions) == sync.LANDED
    upstream_commit(root, {"u2.txt": "u2\n"}, "upstream again")
    clean = merge_upstream(checkout)
    actions2 = staged_green(root, checkout, clean)
    assert gate(checkout, clean, actions2) == sync.LANDED
    git(checkout, "fetch", "-q", "origin")
    capsys.readouterr()
    assert sync.main(["--repo", str(checkout), "--classify", head]) == 0
    out = capsys.readouterr().out
    assert f"CLASSIFY {head[:10]}" in out and ": fork-authored" in out and "shared.txt" in out, out
    assert sync.main(["--repo", str(checkout), "--classify", clean]) == 0
    out = capsys.readouterr().out
    assert f"CLASSIFY {clean[:10]}" in out and ": mechanical" in out, out


def test_a_ship_tool_without_the_review_entry_points_refuses(world, land_stub, monkeypatch, tmp_path, capsys):
    """The installed hermes-core-wt-land is checked against SHIP_GATE_CONTRACT before use: a drifted tool refuses."""
    root, checkout = world
    head = conflict_sync(root, checkout)
    actions = staged_green(root, checkout, head)
    old = tmp_path / "old-land"
    old.write_text(STUB_LAND.replace("def review_reply_guard(gh, slug, pr, review, reply)",
                                     "def review_reply_guard(gh, slug, pr, review)"))
    monkeypatch.setenv("HERMES_CORE_WT_LAND", str(old))
    capsys.readouterr()
    assert gate(checkout, head, actions) == sync.REFUSED
    assert "review_reply_guard lacks reply" in capsys.readouterr().out
    assert review.contract_errors(review.load_land_tool(str(tmp_path / "hermes-core-wt-land"))) == []


# --- Hardening (t_01a1d6cc89d51f84): each fix has a named test its revert fails ----------------------------------

@pytest.mark.parametrize("url", ["https://github.com/someone/hermes-agent.git", "local-path"])
def test_an_upstream_remote_that_is_not_the_real_upstream_refuses(world, land_stub, capsys, url):
    """An ``upstream`` remote naming a fork that holds the conflict-resolution merge would make that merge look
    like upstream's own (excluded from the range: a mechanical sync landing with no review). It refuses."""
    root, checkout = world
    head = conflict_sync(root, checkout)
    before = main_of(root)
    actions = staged_green(root, checkout, head)
    forkish = root / "forkish.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(forkish)], check=True)
    git(checkout, "push", "-q", str(forkish), f"{head}:refs/heads/main")
    if url == "local-path":
        url = str(forkish)
    else:
        git(checkout, "config", f"url.{forkish}.insteadOf", url)
    git(checkout, "remote", "set-url", "upstream", url)
    git(checkout, "fetch", "-q", "upstream")
    assert git(checkout, "rev-parse", "upstream/main") == head  # the whole sync now looks like upstream's
    capsys.readouterr()

    assert gate(checkout, head, actions) == sync.REFUSED
    out = capsys.readouterr().out
    assert main_of(root) == before
    assert f"REFUSED: upstream sync evidence/publication failed: cannot classify {head[:10]}" in out, out
    assert f"is {url}, not github.com/NousResearch/hermes-agent" in out, out
    assert land_stub.read()["gates"] == []
    assert sync.main(["--repo", str(checkout), "--classify", head]) == sync.REFUSED
    assert "not github.com/NousResearch/hermes-agent" in capsys.readouterr().out


def test_a_local_upstream_ref_moved_onto_fork_commits_is_refetched(world, land_stub, capsys):
    """The right URL, but a local upstream/main pointing at the fork's sync head: the ref is re-fetched from the
    real upstream, so the conflict resolution is still fork-authored and parks for review."""
    root, checkout = world
    head = conflict_sync(root, checkout)
    before = main_of(root)
    actions = staged_green(root, checkout, head)
    git(checkout, "update-ref", "refs/remotes/upstream/main", head)
    capsys.readouterr()
    assert gate(checkout, head, actions) == sync.NOT_YET
    out = capsys.readouterr().out
    assert main_of(root) == before
    assert "fork-authored" in out and "shared.txt" in out, out
    assert git(checkout, "rev-parse", "upstream/main") == git(root / "upstream.git", "rev-parse", "main")


def test_an_unexpected_ship_tool_error_refuses_naming_it(world, land_stub, monkeypatch, tmp_path, capsys):
    """A ship-tool exception other than ReviewRefused / ReviewCIStopped / SharedQuotaBrake is REFUSED with its
    name, not a traceback out of advance(); nothing merges."""
    root, checkout = world
    head = conflict_sync(root, checkout)
    before = main_of(root)
    actions = staged_green(root, checkout, head)
    broken = tmp_path / "broken-land"
    broken.write_text(STUB_LAND.replace('def review_gate(gh, slug, pr, sha, branch, base, review_task, impl_task=""):\n',
                                        'def review_gate(gh, slug, pr, sha, branch, base, review_task, impl_task=""):\n'
                                        '    raise RuntimeError("ship tool fault")\n'))
    monkeypatch.setenv("HERMES_CORE_WT_LAND", str(broken))
    capsys.readouterr()
    assert gate(checkout, head, actions) == sync.REFUSED
    out = capsys.readouterr().out
    assert main_of(root) == before  # GitHub's merge never ran: main is exactly as it was
    assert "REFUSED: " in out and "review gate failed: RuntimeError: ship tool fault" in out, out


def test_impl_task_comes_from_the_flag_only(world, land_stub, monkeypatch):
    """--impl-task (ShipReview(impl_task=...)) is the only source; no HERMES_* env var duplicates it."""
    root, checkout = world
    head = conflict_sync(root, checkout)
    actions = staged_green(root, checkout, head)
    monkeypatch.setenv("HERMES_UPSTREAM_SYNC_IMPL_TASK", "t_from_env")
    assert gate(checkout, head, actions) == sync.NOT_YET
    assert land_stub.read()["gates"][-1][5] == ""
    assert gate(checkout, head, actions, review=review.ShipReview(checkout, impl_task="t_from_flag")) == sync.NOT_YET
    assert land_stub.read()["gates"][-1][5] == "t_from_flag"

