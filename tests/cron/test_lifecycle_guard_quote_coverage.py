"""Overlapping quoted output spans must not recursively re-enumerate covered pairs."""
import itertools
import os
import shlex
import subprocess

import pytest

from cron import lifecycle_guard as guard
from cron.lifecycle_guard_output import iter_printed_shell_payloads, quote_scope
from tools.environments.local import build_subprocess_env


def test_repeated_nested_quotes_fit_existing_walk_budget(monkeypatch, tmp_path):
    # Reduced from a wrapper containing many quoted programs, each with a sink.
    # Unknown producers stay visible; there is no executable-name exemption.
    command = "\n".join(f'''item{i}="producer 'value{i}' | bash"''' for i in range(20))
    monkeypatch.setattr(guard, "_MAX_LIFECYCLE_SCAN_LINES", 150)
    assert guard.scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


def test_literal_coverage_does_not_remove_budget_enforcement(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "_MAX_LIFECYCLE_SCAN_LINES", 20)
    command = "\n".join(f'''item{i}="producer 'value{i}' | bash"''' for i in range(20))
    unsafe, reason = guard.scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe and reason and "20-line scan cap" in reason


def test_payload_provenance_distinguishes_decoding_and_argument_assembly():
    payloads = list(iter_printed_shell_payloads(r'''producer 'x\x27y\x27z' | bash'''))
    assert (r"x\x27y\x27z", True) in payloads
    assert ("x'y'z", False) in payloads
    assert ("bash ./target.sh", False) in list(
        iter_printed_shell_payloads("echo bash ./target.sh | bash", include_quoted=False))


@pytest.mark.parametrize("changed", ["cwd", "executed", "aliases", "mask"])
def test_changed_interpretation_reexpands_covered_quotes(monkeypatch, tmp_path, changed):
    budget = guard._LifecycleScanBudget()
    cwd, executed = str(tmp_path), True
    covered = quote_scope(cwd, executed, budget)
    if changed == "cwd":
        cwd = str(tmp_path / "other")
    elif changed == "executed":
        executed = False
    elif changed == "aliases":
        budget.alias_names = None
    else:
        budget.mask_heredocs = False
    # No lifecycle spelling in the scanned text: only the referenced file contains it.
    target = tmp_path / "target.sh"
    target.write_text("hermes gateway restart\n")
    command = f'''producer '{target}' | bash'''
    assert guard._contains_unsafe_gateway_action(
        command, cwd=cwd, depth=0, visited=set(), budget=budget,
        executed=executed, covered_quote_scope=covered)


@pytest.mark.parametrize("prefix", ["", "env ", "command "])
@pytest.mark.parametrize("shell", ["bash", "bash -l", "sh"])
@pytest.mark.parametrize("producer", ["printf '%s\\n'", "echo"])
def test_real_nested_output_wrappers_stay_blocked(tmp_path, prefix, shell, producer):
    target = tmp_path / "target.sh"
    marker = tmp_path / "marker"
    target.write_text("hermes gateway 'restart'\n")
    fake = tmp_path / "hermes"
    fake.write_text('#!/bin/sh\nprintf reached > "$MARKER"\n')
    fake.chmod(0o755)
    # A quoted action defeats the direct spelling regex; the file walk is necessary.
    inner = f"{producer} {shlex.quote('bash ' + str(target))} | {shell}"
    command = f"{prefix}{producer} {shlex.quote(inner)} | {shell}"
    env = build_subprocess_env()
    env.update(PATH=f"{tmp_path}:" + os.environ["PATH"], MARKER=str(marker))
    # Login shells reset PATH: define the fake command in a fixture BASH_ENV too.
    setup = tmp_path / "env.sh"
    setup.write_text(f"hermes() {{ printf reached > {shlex.quote(str(marker))}; }}\nexport -f hermes\n")
    env["BASH_ENV"] = str(setup)
    result = subprocess.run(["bash", "-c", command], env=env, capture_output=True,
                            text=True, encoding="utf-8", timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "reached"
    assert guard.scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (True, None)


def test_quote_containment_for_generated_literal_shapes():
    from cron.lifecycle_guard_output import _QUOTED_LITERAL_RES

    for chars in itertools.product("'\"x\\", repeat=6):
        text = "".join(chars)
        spans = {match[1] for pattern in _QUOTED_LITERAL_RES for match in pattern.finditer(text)}
        for span in spans:
            nested = {match[1] for pattern in _QUOTED_LITERAL_RES for match in pattern.finditer(span)}
            assert nested <= spans
