"""A budget refusal names the file and the cap it hit.

A gateway terminal ran a 950 KB, 17,340-line runner (under 1 MiB, over the
16384-line walk cap) and got only "the scan budget was exhausted (text at
depth 1)": the agent could not tell which file or which limit to fix. The
verdict is unchanged; only the refusal text is pinned here.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from cron import lifecycle_guard
from cron.lifecycle_guard import scan_gateway_lifecycle


def _executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _wc_l(path: Path) -> int:
    """What `wc -l` prints: the file's newline count."""
    return path.read_bytes().count(b"\n")


def test_line_cap_refusal_names_script_and_cap(tmp_path):
    script = _executable(tmp_path / "hermes-sync", "#!/bin/sh\n" + "echo ok\n" * 17000)

    unsafe, refusal = scan_gateway_lifecycle(f"{shlex.quote(str(script))} arg")

    assert unsafe is True
    assert refusal is not None
    assert str(script) in refusal
    assert _wc_l(script) == 17001
    assert "has 17001 lines, over the 16384-line scan cap (text at depth 1)" in refusal


def test_line_count_without_trailing_newline_counts_the_last_line(monkeypatch, tmp_path):
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINES", 4)
    script = _executable(tmp_path / "s.sh", "echo ok\n" * 5 + "echo last")  # wc -l says 5

    unsafe, refusal = scan_gateway_lifecycle(shlex.quote(str(script)))

    assert unsafe is True
    assert refusal is not None
    assert f"`{script}` has 6 lines, over the 4-line scan cap" in refusal


def test_line_count_at_the_cap_names_the_charged_count(monkeypatch):
    """Verdict unchanged: the budget charges the empty line after a final newline, so text with
    exactly the cap's own lines is still refused, and the message says why."""
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINES", 3)

    assert scan_gateway_lifecycle("one\ntwo\nthree\n") == (
        True,
        "the scan budget was exhausted: the command has 3 lines (4 counted), "
        "over the 3-line scan cap (text at depth 0)",
    )


def test_line_cap_refusal_names_remaining_budget(monkeypatch, tmp_path):
    """A file under the cap that overruns what earlier text left says so."""
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINES", 10)
    script = _executable(tmp_path / "s.sh", "echo ok\n" * 9)  # 9 lines, charged as 10

    unsafe, refusal = scan_gateway_lifecycle(f"true\ntrue\n{shlex.quote(str(script))}")

    assert unsafe is True
    assert refusal is not None
    assert str(script) in refusal
    assert "has 9 lines, over the 7 lines left of the 10-line scan cap" in refusal


def test_cap_line_file_after_earlier_text_is_over_what_is_left(monkeypatch, tmp_path):
    """A file of exactly cap lines charges cap+1, but its own count is not over the cap: after
    earlier text spent part of the budget the message names what was left, not the whole cap."""
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINES", 10)
    script = _executable(tmp_path / "s.sh", "echo ok\n" * 10)

    unsafe, refusal = scan_gateway_lifecycle(f"true\ntrue\n{shlex.quote(str(script))}")

    assert unsafe is True
    assert refusal is not None
    assert _wc_l(script) == 10
    assert "has 10 lines, over the 7 lines left of the 10-line scan cap" in refusal


def test_long_line_refusal_names_script_and_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINE_BYTES", 512)
    script = _executable(tmp_path / "long.sh", "echo " + "x" * 600 + "\n")

    unsafe, refusal = scan_gateway_lifecycle(f"bash {shlex.quote(str(script))}")

    assert unsafe is True
    assert refusal is not None
    assert str(script) in refusal
    assert "605-character line, over the 512-character per-line scan cap" in refusal


def test_byte_cap_refusal_names_script_and_cap(monkeypatch, tmp_path):
    """The bounded reader admits bytes it reads raw; text decoded with
    replacement characters can still exceed the byte budget in charge_text."""
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINE_BYTES", 10_000)
    script = tmp_path / "s.sh"
    script.write_text("echo ok\n", encoding="utf-8")
    real_read = lifecycle_guard._read_referenced_script

    def inflated(path, max_bytes=None):
        if Path(path) == script:
            return "é" * 600, False  # 1200 UTF-8 bytes
        return real_read(path, max_bytes=max_bytes)

    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_BYTES", 1000)
    monkeypatch.setattr(lifecycle_guard, "_read_referenced_script", inflated)
    root = f"bash {shlex.quote(str(script))}"

    unsafe, refusal = scan_gateway_lifecycle(root)

    assert unsafe is True
    assert refusal is not None
    assert str(script) in refusal
    assert "is 1200 bytes, over the 1000-byte scan cap (text at depth 1)" in refusal


def test_root_command_refusal_names_the_command_and_cap(monkeypatch):
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINES", 2)

    unsafe, refusal = scan_gateway_lifecycle("one\ntwo\nthree")

    assert unsafe is True
    assert refusal == (
        "the scan budget was exhausted: the command has 3 lines, "
        "over the 2-line scan cap (text at depth 0)"
    )


def test_root_char_count_refusal_is_a_lower_bound(monkeypatch):
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_BYTES", 8)

    assert scan_gateway_lifecycle("x" * 9) == (
        True,
        "the scan budget was exhausted: the command is at least 9 bytes, "
        "over the 8-byte scan cap (text at depth 0)",
    )


@pytest.mark.parametrize("suffix", [".sh", ".py"])
def test_cron_refusal_names_prompt_and_script(monkeypatch, tmp_path, suffix):
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINES", 4)
    script = tmp_path / f"job{suffix}"
    script.write_text("x = 1\n" * 6, encoding="utf-8")
    assert _wc_l(script) == 6

    with pytest.raises(lifecycle_guard.GatewayLifecycleBlocked) as excinfo:
        lifecycle_guard.check_gateway_lifecycle("run report", str(script))
    assert f"the job prompt plus `{script}` has 7 lines, over the 4-line scan cap" in str(excinfo.value)

    # No prompt: the join newline is charged but is not a line of the script.
    for prompt in ("", None):
        with pytest.raises(lifecycle_guard.GatewayLifecycleBlocked) as excinfo:
            lifecycle_guard.check_gateway_lifecycle(prompt, str(script))
        assert f": `{script}` has 6 lines, over the 4-line scan cap" in str(excinfo.value)


@pytest.mark.parametrize("suffix", [".sh", ".py"])
def test_cron_script_without_prompt_verdict_still_charges_the_join(monkeypatch, tmp_path, suffix):
    """Verdict unchanged: a 6-line script with no prompt is charged 8 lines, so a 7-line cap
    refuses it, and the message names its own 6 lines with the charged count."""
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINES", 7)
    script = tmp_path / f"job{suffix}"
    script.write_text("x = 1\n" * 6, encoding="utf-8")

    with pytest.raises(lifecycle_guard.GatewayLifecycleBlocked) as excinfo:
        lifecycle_guard.check_gateway_lifecycle("", str(script))
    assert f": `{script}` has 6 lines (8 counted), over the 7-line scan cap" in str(excinfo.value)

    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINES", 8)
    lifecycle_guard.check_gateway_lifecycle("", str(script))


def test_scan_budget_summary_formats_sizes_exactly():
    assert lifecycle_guard._format_size(1024 * 1024) == "1 MiB"
    assert lifecycle_guard._format_size(64 * 1024) == "64 KiB"
    assert lifecycle_guard._format_size(1536) == "1536 bytes"
    assert lifecycle_guard._format_size(512) == "512 bytes"


def test_non_text_budget_refusal_wording_is_unchanged():
    """Paths/remote-read exhaustion carries no text overrun, even after a text charge failed
    earlier in the same budget."""
    budget = lifecycle_guard._LifecycleScanBudget()
    budget.overrun = "has 9 lines, over the 2-line scan cap"
    assert lifecycle_guard._budget_exhausted(budget, "paths", 1, source="`/x`") is True
    assert budget.refusal == "the scan budget was exhausted (paths at depth 1)"


def test_sh_c_payload_inside_script_keeps_script_name(monkeypatch, tmp_path):
    """Text from a `sh -c` payload in a script is still that script's text: the payload is
    charged again after its script, so a cumulative cap can run out on the payload."""
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINES", 20)
    script = _executable(tmp_path / "wrap.sh", "sh -c '" + "true\n" * 10 + "'\n")  # 12 lines

    unsafe, refusal = scan_gateway_lifecycle(f"bash {shlex.quote(str(script))}")

    assert unsafe is True
    assert refusal is not None
    assert refusal.startswith(f"`{script}` has ")
    assert "lines left of the 20-line scan cap (text at depth 2)" in refusal


def test_mentioned_script_over_budget_is_not_a_refusal(monkeypatch, tmp_path):
    """Verdict unchanged: an over-cap file only mentioned in inert text is allowed."""
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINES", 4)
    script = tmp_path / "many.sh"
    script.write_text("echo ok\n" * 10, encoding="utf-8")

    assert scan_gateway_lifecycle(f"echo {shlex.quote(str(script))}") == (False, None)


@pytest.mark.parametrize("entry", ["cron", "terminal"])
def test_caller_message_states_the_real_scan_budget(entry, tmp_path, monkeypatch):
    script = _executable(tmp_path / "hermes-sync", "#!/bin/sh\n" + "echo ok\n" * 17000)
    if entry == "cron":
        with pytest.raises(lifecycle_guard.GatewayLifecycleBlocked) as excinfo:
            lifecycle_guard.check_gateway_lifecycle("", str(script))
        message = str(excinfo.value)
    else:
        from tools.terminal_tool_guards import gateway_lifecycle_block

        monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process", lambda: True)
        result = gateway_lifecycle_block(
            command=f"{shlex.quote(str(script))} core-mark-deploy abc",
            env=SimpleNamespace(cwd=str(tmp_path)), env_type="local",
            cwd=str(tmp_path), workdir=str(tmp_path), session_key="budget-refusal-test",
        )
        assert result is not None
        message = json.loads(result)["error"]
    assert "could not scan" in message
    assert str(script) in message
    assert "lines, over the 16384-line scan cap" in message, message
    assert "within the scan budget (1 MiB, 16384 lines, 64 KiB per line)" in message
    assert "under 1 MiB" not in message
