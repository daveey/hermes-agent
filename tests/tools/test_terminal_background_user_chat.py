"""Agent-created titled chats must use Desktop routing, not detached CLI ownership."""
import json
from unittest.mock import patch

import pytest

from tools.terminal_tool_guards import background_user_chat_block

INCIDENT = (
    "hermes chat -Q --continue 'AWS runner capacity: dedicated EC2 CI' "
    "--create-if-missing --query-file /scratch/query.txt --run-budget 28800"
)


@pytest.mark.parametrize("command", [
    INCIDENT,
    "env HERMES_HOME=/scratch/hh /opt/bin/hermes --profile work chat "
    "--create-if-missing --continue='A title' -q 'hello'",
    "cd /scratch && " + INCIDENT,
    "nohup " + INCIDENT,
    "bash -lc \"hermes chat -c 'A title' --create-if-missing -q hello\"",
    "python3 -m hermes_cli.main chat -c title --create-if-missing -Q",
    "hermes chat -q '--help' -c title --create-if-missing",
    "hermes chat -c title --create-if-missing --source tool",
    "hermes chat --continue=title --create-if-missing; echo --help",
])
def test_rejects_only_executed_titled_upsert(command):
    result = json.loads(background_user_chat_block(command, background=True))
    assert result["status"] == "blocked"
    assert result["exit_code"] == 1
    assert "cross_session" in result["error"]
    assert "session.create" in result["error"]
    assert "prompt.submit" in result["error"]
    assert "--source tool" in result["error"]


@pytest.mark.parametrize("command", [
    "hermes chat -q 'independent worker' --source tool",
    "hermes chat --query-file /scratch/job --run-budget 28800",
    "hermes chat --resume worker-id -q continue",
    "hermes chat --continue worker -q continue",
    "hermes chat --continue --create-if-missing -q hello",
    "hermes chat -q '--continue TITLE --create-if-missing'",
    "hermes chat -c title -q '--create-if-missing'",
    "hermes chat --create-if-missing -q '--continue=title'",
    "echo hermes chat --continue title --create-if-missing",
    "printf '%s' 'hermes chat -c title --create-if-missing'",
    "hermes chat -c title --create-if-missing --help",
    "hermes chat -- --continue title --create-if-missing",
    "cat <<'EOF'\nhermes chat --continue title --create-if-missing\nEOF\n",
    "python3 -c \"print('hermes chat --continue title --create-if-missing')\"",
])
def test_preserves_workers_help_and_inert_text(command):
    assert background_user_chat_block(command, background=True) is None


def test_foreground_cli_is_not_prohibited():
    assert background_user_chat_block(INCIDENT, background=False) is None


@pytest.mark.parametrize("options", [
    {"background": True},
    {"background": True, "persist_on_release": True},
    {"background": True, "pty": True, "force": True},
    {"timeout": 28800},
])
def test_terminal_refuses_before_backend_or_spawn(options):
    from tools.terminal_tool import terminal_tool

    with patch("tools.terminal_tool._acquire_env") as acquire, \
         patch("tools.terminal_tool.spawn_background_process") as spawn:
        result = json.loads(terminal_tool(INCIDENT, **options))
    assert result["status"] == "blocked"
    acquire.assert_not_called()
    spawn.assert_not_called()
