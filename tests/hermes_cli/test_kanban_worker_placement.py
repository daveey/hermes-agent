"""Frozen worker-first policy: conservative portable-card boundary."""
import pytest

from hermes_cli import kanban_placement as placement
from tests.hermes_cli import hub_judge_support

hub_judge = hub_judge_support.hub_judge


@pytest.mark.parametrize("title", ["Summarize a public paper", "Run the full test suite"])
def test_portable_reason_accepts_light_and_heavy_scratch_work(title):
    assert placement.portable_reason({"title": title, "requires": ["terminal", "file"]}, {}) is None


@pytest.mark.parametrize("fields", [
    {"workspace_kind": "dir"}, {"workspace_kind": "worktree"}, {"workspace_path": "/checkout"},
    {"board": "another"}, {"assignee": "unlisted"}, {"project": "project"},
    {"requires": ["host_repo"]}, {"requires": ["ssh:host"]}, {"requires": ["unknown"]},
    {"needs": ["ssh:host"]},
    {"title": "Edit ~/.hermes/config.yaml"}, {"title": "Inspect kanban.db"},
    {"title": "Tail /home/ubuntu/.hermes/logs/agent.log"},
    {"title": "Restart the gateway"}, {"title": "Use token-broker"},
    {"title": "Run launchd"}, {"title": "Inspect Chrome on my Mac"},
    {"human_request": {"action": "approve"}}, {"max_retries": 2}, {"tenant": "x"},
])
def test_portable_reason_retains_conservative_exclusions(fields):
    assert placement.portable_reason({"title": "Analyze public data", **fields}, {})
    args = {"title": "Analyze public data", **fields}
    cfg = {"route": {"macbook": "hub"}, "heavy_route": {"macbook": "legacy"}}
    options = {"self_gw": "macbook", "live": lambda _: None, "carries": True,
               "locate": lambda _: {"holders": [("hub", {})]}}
    legacy = placement.decide(args, cfg=cfg, **options)
    pooled = placement.decide(args, cfg={**cfg, "worker_pools": {"macbook": ["worker-1"]}}, **options)
    assert pooled == legacy


@pytest.mark.parametrize("status,reason", [(None, None), ("running", None), ("blocked", "uncarried option")])
def test_explicit_default_initial_status_preserves_portability(status, reason):
    args = {"title": "Analyze public data", "requires": ["terminal"], "initial_status": status}
    before = dict(args)
    assert placement.portable_reason(args, {}) == reason
    result = placement.decide(args, cfg={"worker_pools": {"hub": ["worker-1"]}}, self_gw="hub")
    assert (result.get("placement_policy") == "worker-first-v1") == (reason is None)
    assert args == before


def test_running_status_does_not_hide_other_uncarried_options():
    args = {"title": "Analyze public data", "initial_status": "running", "max_retries": 2}
    assert placement.portable_reason(args, {}) == "uncarried option"


def test_extra_home_marker_remains_an_exclusion():
    assert placement.portable_reason({"title": "Inspect special-device"}, {"extra_home_markers": ["special-device"]})


def test_parent_and_origin_metadata_are_not_mutated():
    args = {"title": "Analyze public data", "parents": ["parent"], "origin_session": "@session:default@gw/id"}
    before = dict(args)
    assert placement.portable_reason(args, {}) is None
    assert args == before


@pytest.mark.parametrize("pool", [[], ["worker-1"], ["worker-1", "worker-2"]])
@pytest.mark.parametrize("title", ["Summarize public data", "Run full test suite"])
def test_pool_precedes_gateway_and_heavy_routes_and_never_falls_back(pool, title):
    cfg = {"worker_pools": {"hub": pool}, "route": {"hub": "other"}, "heavy_route": {"hub": "old-worker"}}
    result = placement.decide({"title": title}, cfg=cfg, self_gw="hub")
    assert result["where"] == "local"  # storage stays here; execution is fenced
    assert result["placement_policy"] == "worker-first-v1"
    assert result["why"] == "waiting_for_worker_capacity"
    assert result.get("runs_on") is None


@pytest.mark.parametrize("fields", [{"placement": "local"}, {"placement": "hub"}, {"runs_on": []}, {"runs_on": ["linux"]}])
def test_explicit_pins_bypass_worker_policy(fields):
    result = placement.decide({"title": "Analyze data", **fields}, cfg={"worker_pools": {"hub": ["worker-1"]}}, self_gw="hub")
    assert "placement_policy" not in result
    if "runs_on" in fields:
        args = {"title": "Analyze data", **fields}
        cfg = {"route": {"macbook": "hub"}}
        options = {"self_gw": "macbook", "live": lambda _: None, "carries": True,
               "locate": lambda _: {"holders": [("hub", {})]}}
        assert placement.decide(args, cfg={**cfg, "worker_pools": {"macbook": ["worker-1"]}}, **options) == placement.decide(args, cfg=cfg, **options)


@pytest.mark.parametrize("fields", [{"title": "Fix the hermes-hub ssh config"}, {"requires": ["ssh:hub"]}])
def test_hub_bound_work_keeps_legacy_route(fields):
    args = {"title": "Analyze data", **fields}
    cfg = {"route": {"macbook": "hub"}}
    options = {"self_gw": "macbook", "live": lambda _: None, "carries": True,
               "locate": lambda _: {"holders": [("hub", {})]}}
    legacy = placement.decide(args, cfg=cfg, **options)
    assert legacy["where"] == "hub"
    assert placement.decide(args, cfg={**cfg, "worker_pools": {"macbook": ["worker-1"]}}, **options) == legacy


def test_courier_and_host_work_bypass_worker_policy(hub_judge):
    cfg = {"worker_pools": {"hub": ["worker-1"]}}
    assert "placement_policy" not in placement.decide({"title": "Analyze data"}, origin_intent=object(), cfg=cfg, self_gw="hub")
    hub_judge.admit_next = False  # a courier apply keeps the origin's route: no judge, no enrollment
    assert "placement_policy" not in placement.decide({"title": "Analyze data"}, origin_intent=object(), cfg=cfg, self_gw="hub")
    assert hub_judge.calls == []
    assert "placement_policy" not in placement.decide({"title": "Inspect ~/code/repo", "requires": ["host_repo"]},
                                                      cfg=cfg, self_gw="hub")


@pytest.mark.parametrize("title", [
    "Fix flaky test in ~/code/hermes/tests/test_x.py",
    "Patch /home/ubuntu/code/fabric-play and open a PR",
    "Branch off ~/.hermes/hermes-agent: git -C ~/.hermes/hermes-agent log origin/main",
    "Update skills/devops/foo/SKILL.md in ~/code/hermes-mods and push",
    "hermes-hub client: retry a 409 from the Board",
    "Fix a typo in ~/code/_wtf/notes.md",
    "Read the hermes-agent tree under ~/.hermes for context",
    # A cd into ~/.hermes then into its code tree is the hermes-agent tree, like cd ~/.hermes/hermes-agent
    # (PR #2585 review 5897242912 nit 2).
    "cd ~/.hermes && cd hermes-agent && git fetch -q origin && git log -1 origin/main",
    "cd ~/.hermes; cd ./hermes-agent/ && git pull --ff-only",
    "cd ~ && cd .hermes && cd hermes-agent && git status",
    # Near-misses of the home-rooted .worktrees marker stay portable.
    "Fix the parser bug in ~/code/babybear/src/parser.py and open a PR",
    "Patch /home/ubuntu/code/askeve/app/routes.py and push",
    "Document ~/code/askeve/.worktrees-notes.md cleanup",
    "Rename ~/code/babybear/worktrees/README.md",
    "Add .worktrees to ~/code/askeve/.gitignore",
    "Clean /tmp/scratch/.worktrees in the test fixture",
])
def test_ordinary_repo_work_enrolls_worker_first(title):
    """Repo paths, the Board CLI name and a bare ~/.hermes are not host needs: workers provision checkouts."""
    assert placement.portable_reason({"title": title, "requires": ["terminal", "file", "git"]}, {}) is None
    result = placement.decide({"title": title, "requires": ["terminal", "file", "git"]},
                              cfg={"worker_pools": {"hub": ["worker-1"]}}, self_gw="hub")
    assert result["placement_policy"] == "worker-first-v1"


@pytest.mark.parametrize("title", [
    "Edit ~/.hermes/config.yaml on this gateway", "Rotate /home/ubuntu/.hermes/.env",
    "Prune ~/.hermes/kanban/workspaces", "Fix the hermes-hub ssh config", "Restart the gateway",
    "Continue in worktree ~/code/_wt/fix-x and push", "cd /home/ubuntu/code/_wt/foo then pytest",
])
@pytest.mark.parametrize("admit", [True, False])
def test_hub_state_work_stays_local_only_when_admitted(title, admit, hub_judge):
    """A hub-state marker only asks the judge; denied work enrolls with the hub-write requirement."""
    hub_judge.admit_next = admit
    result = placement.decide({"title": title}, cfg={"worker_pools": {"hub": ["worker-1"]}}, self_gw="hub")
    assert len(hub_judge.calls) == 1
    assert result["hub_admission"]["admit"] is admit
    if admit:
        assert "placement_policy" not in result
    else:
        assert result["placement_policy"] == "worker-first-v1"
        assert "hub:write" in result["requires"]
        assert result.get("runs_on") is None


@pytest.mark.parametrize("title", [
    # ~/.hermes/skills is host state, not a checkout: curator ledger, usage counts, locks, live-patched skills.
    "On this host run `journalctl --user` to inspect worker OOM kills",
    "Inspect this host's model routing registry and config.yaml",
    "Patch ~/.hermes/skills/devops/kanban-orchestrator/SKILL.md (live edit)",
    "Sum use_count from /home/ubuntu/.hermes/skills/.usage.json",
    # Existing worktrees on this host hold unpushed work a worker cannot see.
    "Continue in ~/code/_wt-remote/fix-x and push",
    "Finish the agent tree at ~/code/hermes-mods/.claude/worktrees/agent-a1 and open a PR",
    "Rebase /home/ubuntu/.hermes/hermes-agent/.worktrees/fix-y onto main",
    # Any home-rooted per-repo .worktrees (kanban t_* trees): 11-03 hub had babybear 1 unpushed commit,
    # askeve 12 trees with dirty files. Follow-up cards name the path in text, not a worktree workspace.
    "Continue in ~/code/babybear/.worktrees/t_50dec368 and push",
    "Finish /home/ubuntu/code/askeve/.worktrees/t_01a18c8d53dc31b2 and open a PR",
    "Commit this host's dirty files in `~/code/askeve/.worktrees/t_01a18c8d53dc31b2`.",
    "List the trees under /home/ubuntu/code/babybear/.worktrees",
    # hermes-hub commands act with this gateway's key and replica; a worker has neither.
    "Run hermes-hub create-task hub --title x", "hermes-hub adopt-task t_1 --session s", "On this host run `hermes-hub sync`",
    "Check hermes-hub requests for the stuck ask",
])
def test_host_specific_state_and_identity_stay_local(title, hub_judge):
    assert placement.portable_reason({"title": title, "requires": ["terminal", "file", "git"]}, {}) is not None
    result = placement.decide({"title": title, "requires": ["terminal", "file", "git"]},
                              cfg={"worker_pools": {"hub": ["worker-1"]}}, self_gw="hub")
    assert "placement_policy" not in result  # admitted (fake judge): stays on the hub
    hub_judge.admit_next = False
    denied = placement.decide({"title": title, "requires": ["terminal", "file", "git"]},
                              cfg={"worker_pools": {"hub": ["worker-1"]}}, self_gw="hub")
    from hermes_cli import kanban_placement_coordination as coord
    if coord.requirements(title)[1]:
        # Every pool worker's claim gate refuses a hermes-hub action it is not attested for (it would act with
        # the worker's own key and replica): enrolling it would strand it ready, so a deny keeps it home.
        assert "placement_policy" not in denied and "runs_on" not in denied
        assert denied["requires"] == ["terminal", "file", "git"]
        assert denied["hub_admission"]["home"] == coord.ACTION_REASON
        return
    assert denied["placement_policy"] == "worker-first-v1"
    assert denied["requires"] == ["terminal", "file", "git", "hub:write"]



def test_placement_note_from_the_origin_is_not_a_host_need():
    """A card the MacBook placed on the hub carries remote_body's note naming Chrome, launchd and ssh as
    the MacBook's own things; the note must not make the card look host-bound on the hub."""
    args = {"title": "Fix flaky kanban test", "requires": ["terminal", "file", "git"]}
    args["body"] = placement.remote_body({"body": "Deflake tests/x.py and open a PR."}, self_gw="macbook", target="hub")
    assert "Chrome" in args["body"] and "launchd" in args["body"]
    assert placement.portable_reason(args, {}) is None
    result = placement.decide(args, cfg={"worker_pools": {"hub": ["worker-1"]}}, self_gw="hub")
    assert result["placement_policy"] == "worker-first-v1"


def test_placement_note_does_not_hide_a_real_host_need_in_the_card_text():
    args = {"title": "Fix flaky kanban test"}
    args["body"] = placement.remote_body({"body": "Reproduce with launchctl list on the Mac."},
                                         self_gw="macbook", target="hub")
    assert placement.portable_reason(args, {}) == "local marker"
