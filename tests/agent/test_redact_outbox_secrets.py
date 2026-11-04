"""Managed-worker adoption secrets (outbox ``pending.json`` ``scope.secret``) never reach tool output.

The secret is ``secrets.token_urlsafe(32)``: 43 base64url chars with no vendor prefix, so the prefix
matcher cannot see it. Terminal and execute_code output run with ``code_file=True`` (source-preserving),
which used to skip every key-based pass, so three shapes leaked into agent transcripts:

1. a Python ``repr`` of the scope (``{'secret': '<43 chars>'}``) printed by a script;
2. the raw JSON journal (``"secret": "<43 chars>"``) printed by ``cat``;
3. ``subprocess.TimeoutExpired`` / ``CalledProcessError`` text echoing an argv that carried the secret
   (``Command '['grep', '-rlF', '<secret>', ...]' timed out``): a bare argv has no key at all.

Every secret here is synthetic. Nothing reads a real worker outbox.
"""

import json
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

from agent.redact import redact_sensitive_text, redact_terminal_output
from tests.tools._child_env_fixtures import child_env  # fixture, used via usefixtures

# Synthetic, shaped like secrets.token_urlsafe(32).
SECRET = "Zq3vK9xP-Lm2Rt8Wn_4Yb7Hc1Df6Gj0Sa5Ue3Io9Pk2"
assert len(SECRET) == 43


def _scope(secret: str = SECRET) -> dict:
    return {"profile_id": "/home/u/.hermes", "session_id": "local-abc", "execution_id": "e1",
            "generation": 1, "pid": 4242, "birth": 476261.83, "secret": secret, "epoch": 113}


def _journal(secret: str = SECRET) -> str:
    # The exact on-disk encoding RuntimeSessionStore writes.
    return json.dumps({"scope": _scope(secret), "next_sequence": 1, "pending": []}, separators=(",", ":"))


@pytest.fixture(autouse=True)
def _redaction_on(monkeypatch):
    monkeypatch.setenv("HERMES_REDACT_SECRETS", "true")


def _timeout_text(argv) -> str:
    return str(subprocess.TimeoutExpired(argv, 30))


class TestSourcePreservingOutput:
    """``code_file=True``: the mode terminal and execute_code output use."""

    def test_scope_repr_is_masked(self):
        out = redact_sensitive_text(repr(_scope()), code_file=True)
        assert SECRET not in out
        assert "'secret': '***'" in out
        assert "'execution_id': 'e1'" in out  # the rest of the scope stays readable

    def test_journal_json_is_masked(self):
        out = redact_sensitive_text(_journal(), code_file=True)
        assert SECRET not in out
        assert json.loads(out)["scope"]["secret"] == "***"

    def test_pretty_printed_journal_is_masked(self):
        out = redact_sensitive_text(json.dumps(json.loads(_journal()), indent=2), code_file=True)
        assert SECRET not in out

    def test_journal_inside_json_string_is_masked(self):
        out = redact_sensitive_text(json.dumps({"output": _journal()}), code_file=True)
        assert SECRET not in out

    def test_file_read_uses_non_reusable_sentinel(self):
        out = redact_sensitive_text(_journal(), file_read=True)
        assert SECRET not in out
        assert "***" not in json.loads(out)["scope"]["secret"]

    @pytest.mark.parametrize("n", range(300))
    def test_every_token_urlsafe_shape_is_masked(self, n):
        value = secrets.token_urlsafe(32)
        for text in (repr(_scope(value)), _journal(value)):
            assert value not in redact_sensitive_text(text, code_file=True)

    @pytest.mark.parametrize("text", [
        "CONFIG = {'BRAVE_API_KEY': 'fixture-value-1234567890'}",
        "{'secret': 'test'}",
        "{'secret': 'not_a_real_secret_value_for_unit_tests'}",
        '{"token": "replace-me-with-your-own-token"}',
        "{'token_count': 'AbCdEfGhIjKlMnOpQrStUvWxYz0123456789'}",
        "{'session_id': 'Zq3vK9xP-Lm2Rt8Wn_4Yb7Hc1Df6Gj0Sa5Ue3Io9Pk2'}",
    ])
    def test_source_fixtures_and_non_secret_keys_are_preserved(self, text):
        assert redact_sensitive_text(text, code_file=True) == text


class TestSubprocessArgvDiagnostics:
    def test_timeout_expired_argv_list_is_masked(self):
        text = _timeout_text(["grep", "-rlF", SECRET, "/home/u/.hermes/kanban/workspaces"])
        out = redact_sensitive_text(text, code_file=True)
        assert SECRET not in out
        assert "'grep', '-rlF', '***'" in out
        assert "timed out after 30 seconds" in out

    def test_timeout_expired_shell_string_is_masked(self):
        out = redact_sensitive_text(_timeout_text(f"grep -rlF {SECRET} ."), code_file=True)
        assert SECRET not in out

    def test_called_process_error_argv_is_masked(self):
        text = str(subprocess.CalledProcessError(1, ["curl", "-H", f"X-Adopt: {SECRET}", "http://x"]))
        assert SECRET not in redact_sensitive_text(text, code_file=True)

    def test_traceback_final_line_is_masked_without_force(self):
        text = ("Traceback (most recent call last):\n  File \"<stdin>\", line 1, in <module>\n"
                f"subprocess.TimeoutExpired: {_timeout_text(['grep', '-rlF', SECRET, '.'])}\n")
        assert SECRET not in redact_terminal_output(text, "python3 probe.py")

    @pytest.mark.parametrize("argv", [
        ["gh", f"--token={SECRET}"],
        f"curl -d secret={SECRET} http://x",
        ["env", f"HERMES_SECRET={SECRET}", "x"],
        f"set -e\necho start\ngrep -rlF {SECRET} .\n",
        ["x"] * 1500 + [SECRET],
    ], ids=["flag-eq", "form-eq", "env-eq", "multi-line-shell", "long-argv"])
    def test_joined_multiline_and_long_argv_are_masked(self, argv):
        for text in (_timeout_text(argv), str(subprocess.CalledProcessError(2, argv))):
            assert SECRET not in redact_sensitive_text(text, code_file=True)

    def test_escaped_json_backslash_run_stays_linear(self):
        import time
        text = "\\" * 40_000 + '\\"token\\": \\"x'
        start = time.perf_counter()
        redact_sensitive_text(text, force=True, code_file=True)
        redact_sensitive_text(text, force=True)
        assert time.perf_counter() - start < 2.0

    def test_execute_code_remote_output_pipeline(self):
        from tools.code_execution_tool import _clean_output
        out, _meta = _clean_output(repr(_scope()) + "\n" + _timeout_text(["grep", SECRET, "."]) + "\n")
        assert SECRET not in out

    def test_two_diagnostics_on_one_line_are_both_masked(self):
        other = SECRET[::-1]
        text = _timeout_text(["a", SECRET]) + "; " + _timeout_text(["b", other])
        out = redact_sensitive_text(text, code_file=True)
        assert SECRET not in out and other not in out

    @pytest.mark.parametrize("build", [
        lambda: "Command '" * 200_000,
        lambda: ("Command '" + "x" * 50 + "' timed out after ") * 40_000,
        lambda: "Command '" + "A" * 1_000_000 + "' timed out after 1 seconds",
    ], ids=["heads", "head-tail-pairs", "one-long-run"])
    def test_hostile_output_stays_linear(self, build):
        import time
        text = build()
        start = time.perf_counter()
        redact_sensitive_text(text, force=True, code_file=True)
        assert time.perf_counter() - start < 2.0

    def test_ordinary_argv_is_preserved(self):
        text = _timeout_text(["git", "-C", "/Users/David/Library/Application Support/Hermes", "cat-file",
                              "-p", "fbd4ed1385a0c2f9b5d7e1a3c4b6d8f0a2c4e6f8", "--batch-check"])
        assert redact_sensitive_text(text, code_file=True) == text


# --- Entry paths: the real terminal tool and the real execute_code tool. ---

def _write_outbox(root: Path) -> Path:
    outbox = root / "worker-outboxes" / "admission-worker_synthetic"
    outbox.mkdir(parents=True)
    path = outbox / "pending.json"
    path.write_text(_journal())
    return path


# The secret is read from the file inside the child, so it never sits in the command we send.
def _scripts(pending: Path) -> dict:
    return {
        "repr": f"import json; print(json.load(open({str(pending)!r}))['scope'])",
        "timeout": (
            "import json, subprocess, sys\n"
            f"s = json.load(open({str(pending)!r}))['scope']['secret']\n"
            "subprocess.run([sys.executable, '-c', 'import time; time.sleep(30)', s], timeout=0.2)\n"
        ),
    }


@pytest.mark.platforms("linux", "macos")
@pytest.mark.usefixtures("child_env")
class TestToolEntryPaths:
    def test_terminal_cat_journal(self, tmp_path):
        from tools.terminal_tool import terminal_tool
        pending = _write_outbox(tmp_path)
        result = terminal_tool(f"cat {pending}", task_id="outbox-redact-cat")
        assert SECRET not in result
        assert "next_sequence" in result

    @pytest.mark.parametrize("shape", ["repr", "timeout"])
    def test_terminal_python(self, tmp_path, shape):
        from tools.terminal_tool import terminal_tool
        pending = _write_outbox(tmp_path)
        script = tmp_path / f"probe_{shape}.py"
        script.write_text(_scripts(pending)[shape])
        result = terminal_tool(f"{sys.executable} {script}", task_id=f"outbox-redact-{shape}")
        assert SECRET not in result
        assert ("'secret': '***'" in result) if shape == "repr" else ("TimeoutExpired" in result)

    @pytest.mark.parametrize("shape", ["repr", "timeout"])
    def test_execute_code(self, tmp_path, shape):
        from unittest.mock import patch
        from tools.code_execution_tool import execute_code
        pending = _write_outbox(tmp_path)
        with patch("tools.code_execution_tool._load_config", return_value={"mode": "project"}):
            result = execute_code(code=_scripts(pending)[shape], task_id=f"outbox-redact-ec-{shape}",
                                  enabled_tools=[], reset=True)
        assert SECRET not in result
        out = json.loads(result)
        assert ("'secret': '***'" in out["output"]) if shape == "repr" else (
            "TimeoutExpired" in (out.get("output", "") + out.get("error", "")))
