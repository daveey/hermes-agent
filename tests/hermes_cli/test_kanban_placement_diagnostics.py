"""Creation front doors must explain structural rejection, without changing placement."""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_placement as placement


@pytest.fixture
def local_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    for name in ("HERMES_KANBAN_TASK", "HERMES_SESSION_ID", "HERMES_SESSION_KEY"):
        monkeypatch.delenv(name, raising=False)
    from tests.tools.test_kanban_create_overlap import register_chat
    monkeypatch.setenv("HERMES_SESSION_ID", register_chat(tmp_path, "20261123_120000_diag_owner"))
    cfg = {"worker_pools": {"hub": ["worker-1"]}, "extra_home_markers": ["special-device"]}
    monkeypatch.setattr(placement, "config", lambda: cfg)
    monkeypatch.setattr(placement.reg, "self_gateway", lambda: "hub")
    monkeypatch.setattr(placement, "_machine_picks_up", lambda name: pytest.fail("unexpected registry lookup"))
    from hermes_cli import kanban_worker_node as node
    monkeypatch.setattr(node, "registry", lambda: pytest.fail("unexpected registry/network lookup"))
    kb.init_db()
    return cfg


def _kept(decision):
    """The legacy keep-on-hub decision; the suite's fake judge admits, adding a ``hub_admission`` receipt."""
    assert decision.get("hub_admission", {}).get("admit", True) is True
    return {k: v for k, v in decision.items() if k != "hub_admission"}


@pytest.mark.parametrize("frontdoor", ["tool", "cli"])
@pytest.mark.parametrize("body,assignee,reason,snippet", [
    ("Inspect special-device", "default", "home gateway marker", "special-device"),
    ("Inspect ~/.hermes/cron/jobs.json", "default", "home gateway marker", "~/.hermes/c"),
    ("Inspect Keychain", "default", "local marker", "keychain"),
    ("<!-- placement:evidence --> special-device <!-- /placement:evidence --> Inspect Keychain",
     "default", "local marker", "keychain"),
    ("", "specialist", "assignee not allowlisted", None),
])
def test_create_rejection_diagnostic(local_board, frontdoor, body, assignee, reason, snippet):
    if frontdoor == "tool":
        from tools import kanban_tools as kt
        out = json.loads(kt._handle_create({"title": "Analyze public data", "body": body,
                                          "assignee": assignee, "requires": []}))
        assert out.get("ok"), out
        tid = out["task_id"]
    else:
        from hermes_cli import kanban as kc
        out = json.loads(kc.run_slash(f"create 'Analyze public data' --assignee {assignee} "
                                      f"--body '{body}' --json"))
        tid = out["id"]
    diagnostic = out["placement"]
    assert diagnostic["where"] == "local"
    assert reason in diagnostic["why"]
    assert "no placement route" not in diagnostic["why"]
    if snippet:
        assert snippet in diagnostic["why"]
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.placement_policy is None
        created = next(e for e in kb.list_events(conn, tid) if e.kind == "created")
        assert created.payload["portable_reason"] == reason


@pytest.mark.parametrize("args,reason", [
    ({"needs": ["gateway:hub"]}, "gateway-scoped need"),
    ({"workspace_kind": "dir"}, "host workspace"),
    ({"board": "other"}, "non-default board"),
    ({"assignee": "specialist"}, "assignee not allowlisted"),
    ({"project": "p"}, "uncarried option"),
    ({"requires": ["host_repo"]}, "uncarried requirement"),
])
def test_routeless_reason_preserves_public_classifier(local_board, args, reason):
    view = {"title": "Analyze public data", **args}
    assert placement.portable_reason(view, local_board) == reason
    out = placement.decide(view, cfg=local_board, self_gw="hub")
    assert out["where"] == "local"
    assert reason in out["why"]
    assert "placement_policy" not in out


def test_marker_diagnostic_uses_filtered_work_text(local_board):
    view = {"title": "Analyze public data", "body": (
        "<!-- placement:evidence --> special-device <!-- /placement:evidence -->\n"
        "Inspect ~/.hermes/cron/jobs.json")}
    out = placement.decide(view, cfg=local_board, self_gw="hub")
    assert "home gateway marker" in out["why"]
    assert "~/.hermes/c" in out["why"]
    assert "special-device" not in out["why"]


def test_portable_pool_and_local_pin_unchanged(local_board):
    view = {"title": "Analyze public data"}
    assert placement.decide(view, cfg=local_board, self_gw="hub") == {
        "where": "local", "placement_policy": "worker-first-v1", "why": "waiting_for_worker_capacity"}
    out = placement.decide({**view, "placement": "local", "body": "Inspect Keychain"},
                           cfg=local_board, self_gw="hub")
    assert _kept(out) == {"where": "local", "why": placement.PINNED_LOCAL_WHY}


@pytest.mark.parametrize("attested", [False, True])
def test_coordination_exemptions_do_not_explain_the_wrong_marker(local_board, monkeypatch, attested):
    from hermes_cli import kanban_placement_coordination as coordination
    monkeypatch.setattr(coordination, "attested", lambda *args: attested)
    view = {"title": "Analyze public data", "body": "Run hermes-hub status; inspect special-device"}
    out = placement.decide(view, cfg=local_board, self_gw="hub")
    expected = "home gateway marker: mentions 'special-device'" if attested else coordination.REASON
    assert _kept(out) == {"where": "local", "why": expected}
    assert placement.portable_reason(view, local_board) == (
        "home gateway marker" if attested else coordination.REASON)


@pytest.mark.parametrize("published", [False, True])
def test_worktree_diagnostic_uses_post_publication_text(local_board, monkeypatch, published):
    from hermes_cli import kanban_published_worktree as worktrees
    monkeypatch.setattr(worktrees, "without_published_worktrees", lambda text: "" if published else text)
    view = {"title": "Analyze public data", "body": "Inspect ~/code/_wt/unpublished"}
    out = placement.decide(view, cfg=local_board, self_gw="hub")
    if published:
        assert out["placement_policy"] == "worker-first-v1"
    else:
        assert _kept(out) == {"where": "local", "why": "home gateway marker: mentions '~/code/_wt/'"}


@pytest.mark.parametrize("args,origin", [({"runs_on": []}, None), ({}, {"origin": "elsewhere"})])
def test_manual_and_courier_fallbacks_not_relabelled(local_board, args, origin):
    out = placement.decide({"title": "Inspect Keychain", **args}, cfg=local_board,
                           self_gw="hub", origin_intent=origin)
    assert _kept(out) == {"where": "local", "why": "no placement route for this gateway"}


def test_invalid_extra_marker_and_carried_evidence(local_board, monkeypatch):
    from hermes_cli import kanban_carried_evidence as carried
    local_board["extra_home_markers"] = ["[invalid", "special-device"]
    monkeypatch.setattr(carried, "without_carried_inputs", lambda text, cfg: text.replace("this host", ""))
    out = placement.decide({"title": "Analyze public data", "body": "this host; inspect special-device"},
                           cfg=local_board, self_gw="hub")
    assert _kept(out) == {"where": "local", "why": "home gateway marker: mentions 'special-device'"}


def test_cli_text_and_idempotent_replay(local_board):
    from hermes_cli import kanban as cli
    command = "create 'Analyze public data' --body 'Inspect special-device' --idempotency-key diagnostics"
    text = cli.run_slash(command)
    assert "Placement: home gateway marker: mentions 'special-device'" in text
    replay = json.loads(cli.run_slash(command + " --json"))
    assert "placement" not in replay  # never attribute a fresh decision to an existing card
    assert "Placement:" not in cli.run_slash(command)


def test_cli_enrollment_and_manual_labels(local_board):
    from hermes_cli import kanban as cli
    queued = json.loads(cli.run_slash("create 'Analyze public data' --assignee default --json"))
    assert queued["placement"] == {"where": "local", "placement_policy": "worker-first-v1",
                                    "why": "waiting_for_worker_capacity"}
    from hermes_cli.kanban_placement_diagnostics import cli_creation
    manual = cli_creation(SimpleNamespace(runs_on=[]), "", None, None, "default",
                          SimpleNamespace(placement_policy=None))
    assert manual == {"where": "local", "runs_on": [], "why": "execution labels specified by the caller"}


def test_cli_diagnostic_judges_the_resolved_assignee(local_board):
    from hermes_cli.kanban_placement_diagnostics import cli_creation

    args = SimpleNamespace(title="Analyze public data", assignee=None, runs_on=None)
    task = SimpleNamespace(assignee="specialist", placement_policy=None)
    out = cli_creation(args, "", None, None, "default", task)
    assert out == {"where": "local", "why": "assignee not allowlisted"}


@pytest.mark.parametrize("module,attr", [("kanban_placement_diagnostics", "local_why"),
                                         ("kanban_worker_placement", "_cli_fields")])
def test_cli_diagnostic_failure_does_not_fail_committed_create(local_board, monkeypatch, module, attr):
    """Field building is diagnostic too: helper drift (e.g. a new keyword-only argument)
    must not turn an already committed create into a CLI crash."""
    import importlib
    import sys

    from hermes_cli import kanban as cli
    target = importlib.import_module(f"hermes_cli.{module}")
    real = getattr(target, attr)
    calls = []

    def broken(*args, **kwargs):
        if sys._getframe(1).f_code.co_name != "cli_creation":
            return real(*args, **kwargs)  # cli_policy/cli_audit judge the same fields before the commit
        calls.append(attr)
        raise ValueError("broken diagnostic")
    monkeypatch.setattr(target, attr, broken)
    out = json.loads(cli.run_slash("create 'Inspect special-device' --assignee default --json"))
    assert calls == [attr]
    assert out["placement"] == {"where": "local", "why": "portability diagnostic failed"}
    with kbc.connect() as conn:
        assert kb.get_task(conn, out["id"]) is not None
