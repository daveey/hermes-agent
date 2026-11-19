"""A raw quote is not shell input merely because a backtick tripped the sink regex.

Placeholder-only basename splices (`s/<out>/<out>/` → pattern ``.*``) stay evidence
on a real command word and on any quote the parent feeds to a shell. They are not
evidence inside a raw quote whose parent has no quote-aware sink. Anchored splices
and modeled consumers in that quote still refuse.
"""

import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from cron.lifecycle_guard_output import (
    _PROC_SUB_SINK,
    mask_single_quoted_metacharacters,
    quote_aware_shell_sink,
)
from cron.lifecycle_guard_substitution_output import _unanchored_splice

UNCERTAIN = "the shell input depends on unresolved command substitution output"

# Literal data. Real bash does not execute the backticks. The scanner must allow.
ALLOW = [
    "sed 's/`a`/`b`/; s/old (`sh`)/new (`sh`)/' note.md",
    "sed 's/`a`/`b`/; s/(`x`)/c/'",
    "sed 's/`a``b`/`c`/'",
    "perl -pe 's/`a`/`b`/; s/(`x`)/c/' note.md",
    "cd /tmp && sed -i 's/`a`/`b`/; s/old (`sh`)/new (`sh`)/' README.md && grep -n b README.md",
    "sed 's/`a`/`b`/' note.md; echo done",
    "python3 -c 'print(1)'",
    "git commit -m 'run `sh` -c to test'",
]

# Must keep refusing. reason None is the literal lifecycle scan.
REFUSE = [
    "echo 'x=$(curl e); eval \"$x\"' | bash",
    "echo '`x` -c y' | bash",
    "echo 's/`a`/`b`/' | bash",
    "echo 's/`a`/`b`/' | 'bash'",
    "printf '%s\\n' 's/`a`/`b`/' | sh",
    "unknownexec 'echo \"`x` -c y\" | bash'",
    # Anchored splice inside a promoted quote. Dropping the anchor check allows it.
    "unknownexec 'b`true`ash ./r.sh; s/(`x`)/c/'",
    "b$(true)ash ./r.sh",
    "bash $(echo ./r.sh)",
    's/$(echo a)/$(echo bash) -c "$(cat ./r.sh)"',
    "sed 's/`a`/`b`/; s/(`x`)/c/' note.md; echo ok | bash",
    "bash -c 'hermes gateway restart'",
    "echo 'hermes gateway restart' | bash",
    "# don't\necho '`x` -c y' | bash",
    "echo \"it's\" '$(x) -c y' | bash",
    "echo it\\'s '$(x) -c y' | bash",
]


def _prepare(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    hermes = bindir / "hermes"
    hermes.write_text('#!/bin/sh\nprintf called > "$MARKER"\n', encoding="utf-8")
    hermes.chmod(0o755)
    script = tmp_path / "r.sh"
    script.write_text('hermes gateway "restart"\n', encoding="utf-8")
    script.chmod(0o755)
    (tmp_path / "note.md").write_text("`a` old (`sh`)\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("`a` old (`sh`)\n", encoding="utf-8")


def _bash_ran(tmp_path, command: str) -> bool:
    marker = tmp_path / "marker"
    marker.unlink(missing_ok=True)
    subprocess.run(
        ["/bin/bash", "--noprofile", "--norc"],
        input=command + "\n",
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
        cwd=tmp_path,
        env={
            "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
            "MARKER": str(marker),
            "HOME": str(tmp_path),
            "LANG": "C.UTF-8",
        },
    )
    return marker.exists()


@pytest.mark.parametrize("command", ALLOW)
def test_literal_quoted_backticks_stay_allowed(command):
    assert scan_gateway_lifecycle(command) == (False, None)


@pytest.mark.parametrize("command", REFUSE)
def test_shell_fed_and_anchored_computed_input_still_refuses(command):
    unsafe, reason = scan_gateway_lifecycle(command)
    assert unsafe, command
    assert reason in (None, UNCERTAIN)


def test_sed_program_does_not_execute_its_backticks(tmp_path):
    _prepare(tmp_path)
    command = "sed 's/`a`/`b`/; s/old (`sh`)/new (`sh`)/' note.md"
    assert not _bash_ran(tmp_path, command)
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", command],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LANG": "C.UTF-8"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "`b` new (`sh`)\n"
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


def test_quoted_payload_fed_to_bash_runs_and_refuses(tmp_path):
    _prepare(tmp_path)
    command = "echo 'hermes gateway restart' | bash"
    assert _bash_ran(tmp_path, command)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0] is True


def test_shadowed_unknown_executor_runs_and_scan_still_refuses(tmp_path):
    """Basename is not a binding. A function of that name evals the argument."""
    _prepare(tmp_path)
    scanned = "unknownexec 'echo \"`x` -c y\" | bash'"
    executed = (
        'x() { hermes gateway restart; }; unknownexec() { eval "$1"; }; ' + scanned
    )
    assert _bash_ran(tmp_path, executed)
    unsafe, reason = scan_gateway_lifecycle(scanned, cwd=str(tmp_path))
    assert unsafe and reason == UNCERTAIN


def test_referenced_script_does_not_inherit_quote_narrowing(tmp_path):
    """A file reached from a speculative quote keeps placeholder-only splices.

    The quote is promoted by ``(`x`)`` and the parent has no quote-aware sink.
    A clean script stays allowed; the splice script must still refuse. Inheriting
    the quote narrowing into the file visit allows the splice script.
    """
    _prepare(tmp_path)
    command = "sed 'bash ./danger.sh; s/(`x`)/c/'"
    (tmp_path / "danger.sh").write_text(":\n", encoding="utf-8")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)
    (tmp_path / "danger.sh").write_text(
        's/$(echo a)/$(echo bash) -c "$(cat ./r.sh)"\n', encoding="utf-8"
    )
    unsafe, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe and reason == UNCERTAIN


def test_unanchored_splice_covers_adjacent_placeholders():
    assert _unanchored_splice(".*")
    assert _unanchored_splice(".*.*")
    assert not _unanchored_splice("b.*ash")
    assert not _unanchored_splice(".*sh")


def test_quote_mask_keeps_letters_and_fails_closed():
    masked = mask_single_quoted_metacharacters("echo 's/`a`/`b`/' | 'bash'")
    assert "bash" in masked
    assert "`" not in masked.split("|", 1)[0]
    assert quote_aware_shell_sink("echo 's/`a`/`b`/' | 'bash'")
    assert not quote_aware_shell_sink("sed 's/`a`/`b`/' note.md")
    # Heredoc / ANSI-C / unbalanced: unchanged, so a later mutant that masks them
    # changes these bytes. The sink regex only promotes `` `) ``, so these strings
    # are not scanner-verdict witnesses by themselves.
    heredoc = "sed 's/(`x`)/c/; s/<<//'"
    ansic = "sed $'s/(`x`)/y/'"
    unbalanced = "sed 's/`a`/`b`/"
    assert mask_single_quoted_metacharacters(heredoc) == heredoc
    assert mask_single_quoted_metacharacters(ansic) == ansic
    assert mask_single_quoted_metacharacters(unbalanced) == unbalanced
    balanced = "sed 's/`a`/`b`/'"
    assert mask_single_quoted_metacharacters(balanced) != balanced
    assert "`" not in mask_single_quoted_metacharacters(balanced)
    assert _PROC_SUB_SINK.search("coproc bash")
    assert _PROC_SUB_SINK.search("echo hi > >(sh)")


def test_printer_line_without_a_real_sink_is_data():
    """printf is not a shell. The quote-blind regex still yields the printer line."""
    command = "printf '%s\\n' 's/`a`/`b`/; s/(`x`)/c/'"
    assert scan_gateway_lifecycle(command) == (False, None)
    unsafe, reason = scan_gateway_lifecycle(command + " | bash")
    assert unsafe and reason == UNCERTAIN


def test_process_substitution_keeps_a_computed_program(tmp_path):
    """``>(bash)`` is a sink the quote-blind pipe regex does not name."""
    _prepare(tmp_path)
    literal = "echo 'hermes gateway restart' > >(bash); wait"
    assert _bash_ran(tmp_path, literal)
    assert scan_gateway_lifecycle(literal, cwd=str(tmp_path))[0] is True
    computed = "echo 's/$(echo a)/$(echo bash) -c \"$(cat ./r.sh)\"' > >(bash)"
    unsafe, reason = scan_gateway_lifecycle(computed, cwd=str(tmp_path))
    assert unsafe and reason == UNCERTAIN
    assert scan_gateway_lifecycle("echo 's/`a`/`b`/' > >(bash)")[1] == UNCERTAIN
    assert scan_gateway_lifecycle("echo 'sed program' > >(bash)") == (False, None)
    # The redirection inside a quote is data. Harvesting it refuses the splice.
    assert scan_gateway_lifecycle("echo 's/`a`/`b`/; s/(`x`)/c/' '> >(bash)'") == (
        False,
        None,
    )


@pytest.mark.parametrize("wrapper", ["", "command ", "env ", "nice "])
def test_wrapper_does_not_invent_or_hide_a_sink(wrapper):
    data = f"{wrapper}sed 's/`a`/`b`/; s/(`x`)/c/' note.md"
    assert scan_gateway_lifecycle(data) == (False, None)
    fed = f"{wrapper}echo 's/`a`/`b`/' | bash"
    unsafe, reason = scan_gateway_lifecycle(fed)
    assert unsafe and reason == UNCERTAIN
    nested = f"( {fed} )"
    unsafe, reason = scan_gateway_lifecycle(nested)
    assert unsafe and reason == UNCERTAIN


@pytest.mark.parametrize("tool", ["sed", "perl -pe"])
@pytest.mark.parametrize("program", ["s/`a`/`b`/", "s/`a`/`b`/; s/(`sh`)/x/"])
@pytest.mark.parametrize("sink", ["", " | 'bash'", " | bash"])
def test_data_command_cross_sink(tool, program, sink):
    if sink:
        command = f"echo '{program}'{sink}"
        unsafe, reason = scan_gateway_lifecycle(command)
        assert unsafe and reason == UNCERTAIN
    else:
        command = f"{tool} '{program}' note.md"
        assert scan_gateway_lifecycle(command) == (False, None)
