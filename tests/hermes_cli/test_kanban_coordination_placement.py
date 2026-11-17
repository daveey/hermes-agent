"""Coordination tooling is portable only to attested worker destinations."""
import json
import time

import pytest

from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_worker_node as wn


@pytest.fixture
def fleet(monkeypatch):
    cfg = {"worker_pools": {"hub": ["worker"]}, "assignees": ["default"]}
    entry = {"state": "active", "pickup": "explicit", "last_seen": time.time() * 1000,
             "labels": ["capability:coordination-read", "capability:token-broker",
                        "capability:core-ship", "capability:tailnet-exec"]}
    machines = {"worker": entry}
    monkeypatch.setattr(kp.reg, "self_gateway", lambda: "hub")
    monkeypatch.setattr(wn, "registry", lambda: {"machines": machines})
    return cfg, entry, machines


@pytest.fixture
def board_maps_access(monkeypatch):
    """Explicit full Board support for tests of the independent attestation fences."""
    from hermes_cli import kanban_placement_coordination as coord
    monkeypatch.setattr(coord, "BOARD_PLACEABLE_ACCESS", frozenset(coord.ACCESS_LABELS))


def reason(body, cfg, requires=None):
    return kp.portable_reason({"title": "Read coordination evidence", "body": body,
                               "requires": requires}, cfg)


def test_independent_broker_state_path_stays_home(fleet):
    cfg, _, _ = fleet
    assert reason("Inspect ~/.hermes/token-broker", cfg) == "home gateway marker"


def test_board_provisioning_is_not_reader_capability(fleet):
    cfg, _, _ = fleet
    body = ("Worker-local implementation: create board replicas and attach primary/pickup "
            "subscriptions; token-broker exec for gh read")
    assert reason(body, cfg) == "needs home coordination action"


@pytest.mark.parametrize("body", ["run install-boards.py", "hermes kanban boards create runners"])
def test_board_admin_commands_stay_home(fleet, body):
    cfg, _, _ = fleet
    assert reason(body, cfg) == "needs home coordination action"


def test_attested_reader_and_broker_are_portable(fleet):
    cfg, _, _ = fleet
    assert reason("cross_task read; hermes-hub status; token-broker exec for gh read", cfg) is None


def test_missing_reader_is_named_before_enrollment(fleet):
    cfg, entry, _ = fleet
    entry["labels"] = []
    assert reason("cross_task read", cfg) == "needs worker coordination capability"


@pytest.mark.parametrize("field,value", [
    ("state", "disabled"), ("pickup", None), ("hold", True),
    ("last_seen", 0), ("last_seen", float("inf")), ("last_seen", "invalid"),
    ("last_seen", (time.time() + 3600) * 1000), ("labels", "capability:coordination-read"),
])
def test_unusable_attestation_fails_closed(fleet, field, value):
    cfg, entry, _ = fleet
    entry[field] = value
    assert reason("cross_task read", cfg) == "needs worker coordination capability"


def test_every_member_must_carry_the_capability(fleet):
    cfg, entry, machines = fleet
    cfg["worker_pools"]["hub"].append("other")
    machines["other"] = {**entry, "labels": []}  # fresh, active, unheld: it could take the card
    assert reason("cross_task read", cfg) == "needs worker coordination capability"


# --- kanban-1663abf6ab: only members that could take the card are judged -----------------------------------
@pytest.mark.parametrize("field,value", [
    ("hold", True), ("state", "disabled"), ("pickup", None), ("last_seen", 0),
    ("last_seen", (time.time() + 3600) * 1000),
], ids=["held", "inactive", "pickup_off", "stale", "future"])
def test_unusable_member_is_skipped_not_a_refusal(fleet, board_maps_access, field, value):
    cfg, entry, machines = fleet
    cfg["worker_pools"]["hub"] = ["held", "worker"]
    machines["held"] = {**entry, "labels": [], field: value}
    assert reason("cross_task read; token-broker exec for gh read", cfg) is None
    assert reason("ssh mini and read the log", cfg, ["ssh:mini"]) == "needs worker coordination capability"
    entry["labels"].append("capability:ssh-mini")
    assert reason("ssh mini and read the log", cfg, ["ssh:mini"]) is None


def test_every_member_held_is_not_attested(fleet):
    cfg, entry, machines = fleet
    cfg["worker_pools"]["hub"] = ["a", "worker"]
    machines["a"] = {**entry, "hold": True}
    entry["hold"] = True
    assert reason("cross_task read", cfg) == "needs worker coordination capability"


def test_unknown_member_is_skipped_but_malformed_entry_fails_closed(fleet):
    cfg, _, machines = fleet
    cfg["worker_pools"]["hub"] = ["retired", "worker"]
    assert reason("cross_task read", cfg) is None
    machines["retired"] = "corrupt"
    assert reason("cross_task read", cfg) == "needs worker coordination capability"


def test_hub_pool_with_one_held_member_attests(fleet):
    """The 11-12 hub pool: worker-1 held, workers 2-6 carrying every label (readback False before the fix)."""
    from hermes_cli import kanban_placement_coordination as coord
    _, entry, machines = fleet
    pool = ["worker-2", "worker-1", *(f"worker-{n}" for n in range(3, 7))]
    entry["labels"] += ["capability:terminal"]
    machines.update({name: dict(entry) for name in pool})
    machines["worker-1"]["hold"] = True
    for label in ("capability:coordination-read", "capability:token-broker", "capability:terminal"):
        assert coord.attested(pool, {label})
    machines["worker-4"] = {**entry, "labels": []}
    assert not coord.attested(pool, {"capability:token-broker"})


def _home_card(**extra):
    return {"title": "Review PR 1", "body": "token-broker exec for gh; kanban_show the parent",
            "assignee": "default", "workspace_kind": "scratch", "requires": '["terminal"]', **extra}


def test_stranded_names_free_attesting_pool_workers(fleet, monkeypatch):
    from hermes_cli import kanban_placement_coordination as coord
    cfg, entry, machines = fleet
    cfg["carried_requires"] = ["terminal"]
    cfg["worker_pools"]["hub"] = ["busy", "worker", "held"]
    entry.update(max_card_runs=2, metrics={"active_card_runs": 1})
    machines["busy"] = {**entry, "metrics": {"active_card_runs": 2}}
    machines["held"] = {**entry, "hold": True}
    assert coord.stranded(_home_card(), cfg, coord.REASON) == ["worker"]
    assert coord.stranded(_home_card(), cfg, None) == []  # created portable: not this diagnostic
    assert coord.stranded(_home_card(placement_policy="worker-first-v1"), cfg, coord.REASON) == []
    assert coord.stranded(_home_card(body="token-broker; systemctl restart x"), cfg, coord.REASON) == []
    entry["labels"] = []
    assert coord.stranded(_home_card(), cfg, coord.REASON) == []


def test_queue_note_names_a_home_card_the_pool_could_run(fleet, tmp_path, monkeypatch):
    """The read of a hub card stranded by its creation verdict says so, instead of "waiting for a local slot"."""
    from pathlib import Path

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_queue_position as kqp
    cfg, entry, _ = fleet
    entry.update(max_card_runs=2, metrics={"active_card_runs": 0})
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kp, "config", dict)  # created home-only, as under the 11-12 pool
    kb._INITIALIZED_PATHS.discard(str(kb.kanban_db_path(board="default").resolve()))
    kb.init_db()
    with kbc.connect() as conn:
        stuck = kb.create_task(conn, title="Review PR 1", body="token-broker exec for gh", assignee="default",
                               placement_audit={"portable_reason": "needs worker coordination capability"})
        plain = kb.create_task(conn, title="Review PR 2", body="token-broker exec for gh", assignee="default",
                               placement_audit={"portable_reason": None})
        monkeypatch.setattr(kp, "config", lambda: cfg)
        place = kqp.position(conn, kb.get_task(conn, stuck))
        assert place["lane"] == "local" and place["pool_could_run"] == ["worker"]
        note = kqp.describe(place)
        assert "stale creation verdict" in note and "--runs-on machine:worker" in note
        assert "pool_could_run" not in kqp.position(conn, kb.get_task(conn, plain))
        entry["hold"] = True
        assert "pool_could_run" not in kqp.position(conn, kb.get_task(conn, stuck))


@pytest.mark.parametrize("body", [
    "hermes-hub status; hermes-hub create-task other title", "hermes-hub sync",
    "cross_task comment", "cross_task", "hermes-hub unknown-command",
])
def test_write_or_ambiguous_actions_stay_home(fleet, body):
    cfg, _, _ = fleet
    assert reason(body, cfg) is not None


@pytest.mark.parametrize("body", [
    "hermes-hub status; systemctl restart service", "hermes-hub status; ssh server",
    "hermes-hub status; ~/code/_wt/existing", "hermes-hub status; ~/.hermes/state.db",
])
def test_independent_host_markers_remain(fleet, body):
    cfg, _, _ = fleet
    assert reason(body, cfg) is not None


def test_extra_marker_cannot_be_exempted(fleet):
    cfg, _, _ = fleet
    cfg["extra_home_markers"] = [r"hermes-hub"]
    assert reason("hermes-hub status", cfg) == "home gateway marker"


def test_no_pool_cannot_borrow_another_origins_attestation(fleet):
    cfg, _, _ = fleet
    cfg["worker_pools"] = {"another": ["worker"]}
    assert reason("hermes-hub status", cfg) is not None


def test_actual_claim_rechecks_changed_labels(fleet, monkeypatch):
    from hermes_cli import kanban_runs_on_capabilities as rc
    cfg, entry, _ = fleet
    card = {"title": "cross_task read", "placement_policy": "worker-first-v1"}
    assert reason(card["title"], cfg) is None
    assert rc.claim_refusal(card, me="worker") is None
    entry["labels"] = []
    assert rc.claim_refusal(card, me="worker") == "needs worker coordination capability"
    card["placement_policy"] = None
    assert rc.claim_refusal(card, me="worker") is None


def test_execution_rechecks_already_imported_card(fleet, monkeypatch):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_placement as wp
    _, entry, _ = fleet
    monkeypatch.setattr(sync, "board_mode", lambda _: "primary")
    monkeypatch.setattr(sync, "sync_state", lambda _: {"gateway_id": "worker"})
    card = {"title": "cross_task read", "placement_policy": "worker-first-v1",
            "runs_on": '["machine:worker"]'}
    assert wp.execution_refusal(None, card) is None
    entry["labels"] = []
    assert wp.execution_refusal(None, card) == "needs worker coordination capability"


def test_legacy_target_cannot_borrow_pool_labels(fleet):
    cfg, _, _ = fleet
    args = {"title": "Run pytest", "body": "hermes-hub status"}
    assert kp.portable_reason(args, cfg) is None
    cfg["heavy_route"] = {"hub": "worker"}
    assert kp._routeless_heavy(args, kp._Cfg(cfg), "hub", lambda _: True) is None
    # Legacy pins cannot retain a requirement; even the capable target stays excluded.
    cfg["heavy_route"]["hub"] = "other"
    assert kp._routeless_heavy(args, kp._Cfg(cfg), "hub", lambda _: True) is None


@pytest.mark.parametrize("pool", [None, [], "worker", [1]])
def test_malformed_pool_is_not_attested(fleet, pool):
    cfg, _, _ = fleet
    cfg["worker_pools"]["hub"] = pool
    assert reason("cross_task read", cfg) is not None


def test_missing_registry_fails_closed(fleet, monkeypatch):
    cfg, _, _ = fleet
    monkeypatch.setattr(wn, "registry", lambda: None)
    assert reason("cross_task read", cfg) is not None


@pytest.mark.parametrize("skill", ["token-broker", "hermes-mods-repo-core-dev", "patching-local-hermes-workflow"])
def test_skill_pin_requires_attestation(fleet, skill):
    cfg, entry, _ = fleet
    args = {"title": "Implement a fix", "skills": [skill]}
    assert kp.portable_reason(args, cfg) is None
    entry["labels"] = []
    assert kp.portable_reason(args, cfg) == "needs worker coordination capability"


@pytest.mark.parametrize("text", [
    "hermes-hub client: retry a 409 from the Board",
    "Build a `hermes-hub` CLI shim",
    "Fix the `cross_task` endpoint",
    "Live check on\nhermes-hub reports no tools and exits 0",
    "crash storm on hermes-hub) was detected yesterday",
    "hermes-hub error receipt PR75",
    "hosts/hermes-hub/bin/hub-core-update",
    'connection="hermes-hub"',
    "Fix cross_task comments being labeled human through Desktop endpoint",
    "Check cross_task comment179201 wrongly writer_kind=human",
    "Test actual agent cross_task path and human UI path independently",
])
def test_development_or_hostname_mentions_add_no_requirement(fleet, text):
    from hermes_cli import kanban_placement_coordination as coord
    cfg, entry, _ = fleet
    entry["labels"] = []
    assert coord.requirements(text) == (set(), None)
    assert reason(text, cfg) is None


@pytest.mark.parametrize("text", [
    "Run hermes-hub frobnicate", "`hermes-hub frobnicate`",
    "hermes-hub unknown-command", "hermes-hub --new-option",
    "hermes-hub status; hermes-hub create-task title",
    "Use cross_task comment", "cross_task comment task-id",
    'cross_task(action="comment", target="task")',
    "cross_task read; cross_task unknown-command", "cross_task",
])
def test_explicit_execution_is_fail_closed(fleet, text):
    from hermes_cli import kanban_placement_coordination as coord
    assert coord.requirements(text)[1] == coord.ACTION_REASON


@pytest.mark.parametrize("text", [
    "fails with current core and passes with hermes-hub's older core",
    "compare via hermes-hub\u2019s journal output",
    "use hermes-hub's checkout as the base",
    "Run hermes-hub's\nexisting fixtures",
    "verified with cross_task's comment path",
])
def test_possessive_prose_is_not_an_invocation(fleet, text):
    from hermes_cli import kanban_placement_coordination as coord
    cfg, entry, _ = fleet
    entry["labels"] = []
    assert coord.requirements(text) == (set(), None)
    assert reason(text, cfg) is None


@pytest.mark.parametrize("text", [
    "`hermes-hub unknown-cmd`", "run hermes-hub", "run hermes-hub' frobnicate",
    "run hermes-hub's-tool", "use cross_task", "with hermes-hub'sync",
])
def test_possessive_exemption_keeps_invocations_fail_closed(fleet, text):
    from hermes_cli import kanban_placement_coordination as coord
    assert coord.requirements(text)[1] == coord.ACTION_REASON


def test_possessive_exemption_keeps_status_read():
    from hermes_cli import kanban_placement_coordination as coord
    assert coord.requirements("hermes-hub status") == ({"capability:coordination-read"}, None)


@pytest.mark.parametrize("text", ["hermes-hub status", "hermes-hub pull", "cross_task read", "cross_task list"])
def test_each_read_needs_fresh_attestation(fleet, text):
    cfg, entry, _ = fleet
    assert reason(text, cfg) is None
    entry["labels"] = []
    assert reason(text, cfg) == "needs worker coordination capability"


@pytest.mark.parametrize("pools", [None, [], "worker", {}, {"hub": [" "]},
                                        {"hub": ["worker"] * 65}])
def test_missing_or_invalid_current_pool(fleet, pools):
    cfg, _, _ = fleet
    cfg["worker_pools"] = pools
    assert reason("cross_task read", cfg) == "needs worker coordination capability"


@pytest.mark.parametrize("snapshot", [None, [], {}, {"machines": None},
                                           {"machines": []}, {"machines": {}},
                                           {"machines": {"worker": None}}])
def test_invalid_registry_or_missing_destination(fleet, monkeypatch, snapshot):
    cfg, _, _ = fleet
    monkeypatch.setattr(wn, "registry", lambda: snapshot)
    assert reason("cross_task read", cfg) == "needs worker coordination capability"


@pytest.mark.parametrize("field,value", [
    ("labels", None), ("labels", [1, "capability:coordination-read"]),
    ("last_seen", None), ("last_seen", float("nan")),
    ("last_seen", 10**1000), ("last_seen", float("-inf")),
])
def test_malformed_attestation_fields(fleet, field, value):
    cfg, entry, _ = fleet
    entry[field] = value
    assert reason("cross_task read", cfg) == "needs worker coordination capability"


def test_explicit_destination_does_not_borrow_current_pool(fleet):
    from hermes_cli import kanban_placement_coordination as coord
    cfg, _, _ = fleet
    assert coord.check("cross_task read", cfg, target="missing") == coord.REASON
    cfg["worker_pools"] = None
    assert coord.check("cross_task read", cfg, target="worker") is None


def test_marker_exemption_preserves_legacy_and_other_text():
    from hermes_cli import kanban_placement_coordination as coord
    text = "hermes-hub status; hermes-hub sessions; token-broker; ssh server"
    assert coord.marker_text(text) == "; hermes-hub sessions; ; ssh server"


@pytest.mark.parametrize("command", [
    "sync", "sessions", "inbox", "tasks", "where", "links", "request", "requests",
    "inspect-legacy", "reconcile-legacy", "inspect-alias", "retire-alias",
    "adopt-task", "create-task", "depend", "outcomes", "install-watch", "watch", "diff-shared",
])
def test_every_legacy_cli_marker_still_refuses(fleet, command):
    cfg, _, _ = fleet
    assert reason("hermes-hub " + command, cfg) == "needs home coordination action"
    # The old marker permits arbitrary whitespace; narrowing intent detection
    # must never turn a non-exempt old marker into a portable card.
    assert reason("hermes-hub\n" + command, cfg) is not None


def test_ordinary_work_does_not_read_registry(monkeypatch):
    from hermes_cli import kanban_placement_coordination as coord
    def unexpected():
        pytest.fail("ordinary source work must not need registry availability")
    monkeypatch.setattr(wn, "registry", unexpected)
    assert coord.check("Fix cross_task comments in the endpoint", {}) is None
    assert coord.legacy_refusal({"title": "Fix cross_task comments"}) is False


# --- remote access capabilities (operator attestations on the worker's Board labels) -------------------------
ACCESS = ["capability:ssh-mbp", "capability:ssh-mini", "capability:hub-read"]


def test_ssh_card_needs_mac_ssh_attestation(fleet, board_maps_access):
    cfg, entry, _ = fleet
    assert reason("ssh mini and check the watcher output", cfg) == "home gateway marker"
    entry["labels"] += ACCESS
    assert reason("ssh mini and check the watcher output", cfg, ["ssh:mini"]) is None


def test_ssh_attestation_needs_the_declared_mac_key(fleet):
    cfg, entry, _ = fleet
    entry["labels"] += ["capability:ssh-mini"]
    assert reason("SSH to the MacBook for the log", cfg, ["ssh:mbp"]) is not None


@pytest.mark.parametrize("body", ["read the log on the hub", "the hub's log output", "journalctl -u x",
                                    "query kanban.db for the run"])
def test_hub_reads_need_hub_read_attestation(fleet, board_maps_access, body):
    cfg, entry, _ = fleet
    assert reason(body, cfg) == "home gateway marker"
    entry["labels"] += ["capability:hub-read"]
    assert reason(body, cfg, ["hub:read"]) is None


def test_access_labels_do_not_exempt_other_home_markers(fleet):
    cfg, entry, _ = fleet
    entry["labels"] += ACCESS
    for body in ("ssh mini then launchctl via launchd", "on the hub: systemctl restart x",
                 "ssh mini and edit hermes-sync state", "read the log on the hub, then restart the gateway"):
        assert reason(body, cfg) in ("home gateway marker", "local marker"), body


def test_claim_refusal_revalidates_access_labels(fleet):
    from hermes_cli.kanban_placement_coordination import claim_refusal
    _, entry, _ = fleet
    card = {"placement_policy": "worker-first-v1", "title": "t", "body": "ssh mini then read the log",
            "requires": '["ssh:mini"]'}
    assert claim_refusal(card, "worker") == "needs worker coordination capability"
    entry["labels"] += ACCESS
    assert claim_refusal(card, "worker") is None


def test_unattested_access_leaves_cards_home_as_before(fleet, board_maps_access):
    cfg, entry, _ = fleet
    entry["labels"] += ["capability:hub-read"]  # hub reads attested, ssh not
    assert reason("ssh mini and read the log on the hub", cfg) == "home gateway marker"
    assert reason("read the log on the hub", cfg, ["hub:read"]) is None


# --- PR #1330 review: the Board cannot place declared access yet (worker_placement.ts supportedRequirement) ----
@pytest.mark.parametrize("requires", [["ssh:mini"], ["ssh:mbp"]])
def test_declared_access_stays_home_until_the_board_maps_it(fleet, monkeypatch, requires):
    """Every eligible member attests every label, yet the Board would leave an enrolled card queued
    (``task_requirement_unsupported``): keep it home-only, where it at least runs."""
    from hermes_cli import kanban_placement_coordination as coord
    monkeypatch.setattr(coord, "BOARD_PLACEABLE_ACCESS", frozenset({"hub:read", "hub:write"}))
    cfg, entry, machines = fleet
    cfg["carried_requires"] = ["terminal"]
    cfg["worker_pools"]["hub"] = ["held", "worker"]
    machines["held"] = {**entry, "hold": True}
    entry["labels"] += ACCESS
    assert reason("read the log", cfg, requires) == coord.BOARD_REASON
    assert reason("read the log", cfg, ["terminal"]) is None
    card = {"title": "Read the log", "body": "read the log", "assignee": "default", "workspace_kind": "scratch",
            "requires": json.dumps(requires)}
    entry.update(max_card_runs=2, metrics={"active_card_runs": 0})
    assert coord.stranded(card, cfg, coord.REASON) == []
    monkeypatch.setattr(coord, "BOARD_PLACEABLE_ACCESS", frozenset(coord.ACCESS_LABELS))
    assert reason("read the log", cfg, requires) is None
    assert coord.stranded(card, cfg, coord.REASON) == ["worker"]


@pytest.mark.parametrize("requires", [["ssh:mini"], ["ssh:mbp"], ["hub:read"], ["hub:write"],
                                      ["terminal", "file", "hub:read", "hub:write", "ssh:mini", "ssh:mbp"]])
def test_declared_access_is_board_placeable(fleet, requires):
    """Use the real supported-access set, not the broader hypothetical Board fixture."""
    from hermes_cli import kanban_placement_coordination as coord
    cfg, entry, machines = fleet
    cfg["carried_requires"] = ["terminal", "file"]
    cfg["worker_pools"]["hub"] = ["held", "worker"]
    machines["held"] = {**entry, "labels": [], "hold": True}
    assert coord.board_refusal(requires) is None
    labels = coord.access_requirements(requires)
    assert labels == {"capability:" + r.replace(":", "-") for r in requires if r.startswith(("hub:", "ssh:"))}
    entry["labels"] += list(labels)
    assert reason("read the log", cfg, requires) is None
    card = {"placement_policy": "worker-first-v1", "title": "Read the log", "body": "read the log",
            "requires": json.dumps(requires)}
    assert coord.claim_refusal(card, "worker") is None
    for label in labels:
        entry["labels"] = [v for v in entry["labels"] if v != label]
        assert coord.attested(["held", "worker"], labels) is False
        assert coord.claim_refusal(card, "worker") == coord.REASON
        entry["labels"].append(label)
    assert coord.attested(["held", "worker"], labels) is True


def test_board_access_allowlist_is_exact():
    from hermes_cli import kanban_placement_coordination as coord
    assert coord.BOARD_PLACEABLE_ACCESS == {"hub:read", "hub:write", "ssh:mini", "ssh:mbp"}


@pytest.mark.parametrize("access", ["ssh:mini", "ssh:mbp"])
@pytest.mark.parametrize("field,value", [("last_seen", 0), ("hold", True), ("labels", [])])
def test_ssh_claim_refuses_expired_held_or_revoked_attestation(fleet, access, field, value):
    from hermes_cli import kanban_placement_coordination as coord
    cfg, entry, _ = fleet
    entry["labels"] += ACCESS
    card = {"placement_policy": "worker-first-v1", "title": "Read evidence", "requires": [access]}
    assert reason("read evidence", cfg, [access]) is None
    assert coord.claim_refusal(card, "worker") is None
    entry[field] = value
    assert reason("read evidence", cfg, [access]) == coord.REASON
    assert coord.claim_refusal(card, "worker") == coord.REASON


@pytest.mark.parametrize("text", [
    "Read it with `hub-admin 'hermes-hub outcomes --id t_x-v1'`",
    'hub-admin "hermes-hub requests --json"',
    "run hub-admin 'cd ~ && hermes-hub request tasks k comment'",
])
def test_hub_admin_wrapped_action_needs_hub_write(text):
    """hub-admin runs the action on the hub with its own identity: the hub-admin attestation, not a refusal."""
    from hermes_cli import kanban_placement_coordination as coord
    assert coord.requirements(text) == ({"capability:hub-write"}, None)


@pytest.mark.parametrize("text", [
    "hub-admin 'uptime'; hermes-hub outcomes --id t_x",  # closed quote: the action runs on this worker
    "hub-admin 'uptime'\n`hermes-hub outcomes --id t_x`",
    "`hermes-hub outcomes --id t_x` (a worker reads only its own sent requests)",
    "ssh hermes-hub 'hermes-hub outcomes --id t_x'",
])
def test_bare_or_unwrapped_action_still_stays_home(text):
    from hermes_cli import kanban_placement_coordination as coord
    assert coord.requirements(text)[1] == coord.ACTION_REASON


@pytest.mark.parametrize("prefix", ["my-hub-admin", "hermes-hub-admin", "tools/hub-admin", "x.hub-admin",
                                    "my_hub-admin"])
def test_hub_admin_lookalike_names_do_not_open_a_hub_write_quote(prefix):
    """Only the hub-admin command itself runs on the hub: a longer name ending in it is another tool."""
    from hermes_cli import kanban_placement_coordination as coord
    assert coord.requirements(f"{prefix} 'hermes-hub outcomes --id t_x'")[1] == coord.ACTION_REASON
    assert coord.requirements("run hub-admin 'hermes-hub outcomes --id t_x'") == ({"capability:hub-write"}, None)


@pytest.mark.parametrize("text", [
    "~/.hermes/bin/hub-admin 'hermes-hub outcomes --id t_x'",
    "run /home/ubuntu/.hermes/bin/hub-admin 'hermes-hub requests --json'",
    "./hub-admin 'hermes-hub outcomes --id t_x'",
])
def test_path_qualified_hub_admin_stays_home(text):
    """Pinned: the hub-write attestation covers the bare hub-admin on PATH only, so a path-qualified spelling
    is not read as hub-admin and the wrapped action keeps its own verdict (fail closed: stays home)."""
    from hermes_cli import kanban_placement_coordination as coord
    labels, reason = coord.requirements(text)
    assert reason == coord.ACTION_REASON and coord.HUB_WRITE not in labels
