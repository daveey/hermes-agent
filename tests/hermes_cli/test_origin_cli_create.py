"""CLI bridge option parity and offered-channel no-fallback contract."""
import argparse
import json

import pytest

from hermes_cli import kanban as cli, kanban_parser
from tools import origin_cli_bridge as bridge


def parse(*words):
    parser = argparse.ArgumentParser()
    kanban_parser.build_parser(parser.add_subparsers())
    return parser.parse_args(["kanban", "create", "Native child", *words])


@pytest.fixture
def offered(monkeypatch):
    monkeypatch.setenv(bridge.ENV_KEY, "offered")
    monkeypatch.delenv("HERMES_ORIGIN_SESSION", raising=False)
    calls = []
    def request(locator, args):
        calls.append((locator, args))
        return {"ok": True, "task_id": "t_bridge"}
    monkeypatch.setattr(bridge, "request", request)
    def forbidden(*args, **kwargs):
        pytest.fail("offered bridge fell back to direct database create")
    monkeypatch.setattr(cli.kbc, "connect_closing", forbidden)
    return calls


def test_cli_dispatches_to_offered_bridge(offered, capsys):
    assert cli.kanban_command(parse("--json", "--no-requires")) == 0
    assert json.loads(capsys.readouterr().out)["task_id"] == "t_bridge"
    assert offered[0][0] == "offered"
    assert offered[0][1]["placement"] == "local"
    assert offered[0][1]["requires"] == []


def test_cli_maps_options_and_child_body_file(offered, capsys, tmp_path):
    body = tmp_path / "body.txt"
    body.write_text("body\n--not-a-flag", encoding="utf-8")
    args = parse("--json", "--body-file", str(body), "--assignee", "writer", "--parent", "t_parent",
                 "--model", "model-x", "--provider", "provider-x", "--max-runtime", "2m",
                 "--workspace", "scratch", "--initial-status", "running", "--review-required",
                 "--idempotency-key", "cli-key", "--priority", "7", "--skill", "example")
    assert cli.kanban_command(args) == 0
    payload = offered[0][1]
    assert payload["body"] == "body\n--not-a-flag"
    assert payload["parents"] == ["t_parent"]
    assert payload["model"] == "model-x" and payload["provider"] == "provider-x"
    assert payload["max_runtime_seconds"] == 120
    assert payload["workspace_kind"] == "scratch"
    assert payload["initial_status"] == "running" and payload["review_required"]
    assert payload["idempotency_key"] == "cli-key" and payload["priority"] == 7
    assert payload["skills"] == ["example"]
    assert "created_by" not in payload and "session_id" not in payload


@pytest.mark.parametrize("words", [
    ["--origin-session", "@session:default@elsewhere/chat"],
    ["--created-by", "other"], ["--self-work", "{}"],
    ["--force-hub"], ["--hub-reason", "browser"], ["--requires", "terminal"],
    ["--runs-on", "linux"], ["--workspace", "worktree", "--branch", "wt/example"],
    ["--reasoning-effort", "high"],
])
def test_unsupported_options_refuse_without_dispatch(offered, words):
    assert cli.kanban_command(parse(*words)) == 2
    assert offered == []


def test_ambient_origin_refuses_before_dispatch(offered, monkeypatch):
    monkeypatch.setenv("HERMES_ORIGIN_SESSION", "@session:default@elsewhere/chat")
    assert cli.kanban_command(parse()) == 2
    assert offered == []


@pytest.mark.parametrize("locator", ["", "{}", "not-json"])
def test_broken_offer_never_falls_back(offered, monkeypatch, locator):
    monkeypatch.setenv(bridge.ENV_KEY, locator)
    monkeypatch.setattr(bridge, "request", lambda *a: {"ok": False, "outcome": "outcome_unknown"})
    assert cli.kanban_command(parse("--json")) == 2


def test_entrypoint_never_initializes_child_board(offered, monkeypatch):
    def forbidden():
        pytest.fail("child attempted board initialization")
    monkeypatch.setattr(cli.kb, "init_db", forbidden)
    monkeypatch.setattr(cli, "_is_delegated_child_cli_mutation", lambda args: False)
    assert cli.kanban_command(parse("--json")) == 0
    assert len(offered) == 1


def test_no_offer_keeps_legacy_route(monkeypatch):
    from hermes_cli.kanban_cli_native import offered_create
    monkeypatch.delenv(bridge.ENV_KEY, raising=False)
    assert offered_create(parse()) is None


@pytest.mark.parametrize("words", [["--workspace", "invalid"], ["--max-runtime", "invalid"],
                                   ["--body-file", "/nonexistent/native-cli-body"]])
def test_invalid_local_inputs_refuse(offered, words):
    assert cli.kanban_command(parse(*words)) == 2
    assert offered == []


def test_child_launch_policy_is_never_forwarded(offered):
    args = parse("--json")
    args.yolo = True
    args.toolsets = ["all"]
    args.ignore_rules = True
    assert cli.kanban_command(args) == 0
    assert not {"yolo", "toolsets", "ignore_rules"} & offered[0][1].keys()


def test_new_unmapped_option_refuses(offered):
    args = parse()
    args.future_effect = True
    assert cli.kanban_command(args) == 2
    assert offered == []
