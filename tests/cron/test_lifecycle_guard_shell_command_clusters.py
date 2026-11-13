"""A shell's ``-c`` inside a short-option cluster (``bash -ec``, ``-lc``, ``-xc``, ``-ce``).

The guard read only the exact word ``-c`` as the command-string option, so the payload of
``bash -ec '…'`` / ``bash -lc '…'`` was never walked: a script it runs, an oversize or live-SQLite
target, or a script past the walk budget all passed unread (fail-open). Options after ``-c``
(``-c -e '…'``, ``-c -- '…'``) ended the read the same way.
"""

from __future__ import annotations

import shlex

import pytest

from cron import lifecycle_guard
from cron.lifecycle_guard import scan_gateway_lifecycle

# Assembled so this file's own text never carries the literal command.
LIFECYCLE = " ".join(["hermes", "gateway", "re" + "start"])

CLUSTERED_PAYLOAD_SHAPES = [
    "bash -c {payload}",
    "bash -ec {payload}",
    "bash -lc {payload}",
    "bash -xc {payload}",
    "bash -euc {payload}",
    "bash -ce {payload}",
    "bash -euo pipefail -c {payload}",
    "bash -euo pipefail -lc {payload}",
    "bash -c -e {payload}",
    "bash -c -- {payload}",
    "bash -lc -o pipefail {payload}",
    "sh -ec {payload}",
    "dash -ec {payload}",
    "zsh -lc {payload}",
    # zsh's `-O` takes no value, so the next word is the payload.
    "zsh -Oc {payload}",
    "ksh -xc {payload}",
    "sudo bash -lc {payload}",
    "time bash -ec {payload}",
    "if true; then bash -ec {payload}; fi",
]


@pytest.fixture
def paths(tmp_path) -> dict[str, str]:
    restart = tmp_path / "restart.sh"
    restart.write_text(f"#!/bin/sh\necho hi\n{LIFECYCLE}\n", encoding="utf-8")
    big = tmp_path / "big.sh"
    big.write_text("#" * (lifecycle_guard._MAX_REFERENCED_SCRIPT_BYTES + 1), encoding="utf-8")
    clean = tmp_path / "clean.sh"
    clean.write_text("echo ok\n", encoding="utf-8")
    return {name: str(path) for name, path in
            {"restart": restart, "big": big, "clean": clean}.items()}


@pytest.mark.parametrize("shape", CLUSTERED_PAYLOAD_SHAPES)
def test_clustered_c_payload_walks_the_script_it_runs(paths, shape):
    payload = shlex.quote(f"bash {shlex.quote(paths['restart'])}")
    assert scan_gateway_lifecycle(shape.format(payload=payload)) == (True, None)


@pytest.mark.parametrize("shape", CLUSTERED_PAYLOAD_SHAPES)
def test_clustered_c_payload_fails_closed_on_an_oversize_script(paths, shape):
    payload = shlex.quote(f"bash {shlex.quote(paths['big'])}")
    blocked, reason = scan_gateway_lifecycle(shape.format(payload=payload))
    assert blocked is True
    assert reason is not None and "scan cap" in reason


@pytest.mark.parametrize("shape", CLUSTERED_PAYLOAD_SHAPES)
def test_clustered_c_payload_running_a_clean_script_stays_allowed(paths, shape):
    payload = shlex.quote(f"bash {shlex.quote(paths['clean'])}")
    assert scan_gateway_lifecycle(shape.format(payload=payload)) == (False, None)


@pytest.mark.parametrize(
    "template",
    [
        # The payload is inline source, so the shell reads no script and no stdin program.
        "bash -ec 'echo hi' < {restart}",
        "bash -lc 'echo hi' < {restart}",
        "cat {restart} | bash -xc 'cat >/dev/null'",
        # A mention inside the payload is not a run.
        "bash -lc 'echo {restart}'",
        "bash -ec 'grep restart {restart}'",
        # `-o`/`O` values and `+` options hold no `c` flag.
        "bash -o noclobber {clean}",
        "bash +O checkwinsize {clean}",
    ],
)
def test_inline_source_and_mentions_stay_allowed(paths, template):
    quoted = {name: shlex.quote(path) for name, path in paths.items()}
    assert scan_gateway_lifecycle(template.format(**quoted)) == (False, None)


@pytest.mark.parametrize(
    ("command", "payloads"),
    [
        ("bash -c 'a'", ["a"]),
        ("bash -ec 'a'", ["a"]),
        ("bash -lc 'a' arg0 arg1", ["a"]),
        ("bash -euo pipefail -c 'a'", ["a"]),
        ("bash -c -e 'a'", ["a"]),
        ("bash -c -- 'a'", ["a"]),
        ("bash -o errexit -xc 'a'", ["a"]),
        ("zsh -lc 'a'", ["a"]),
        # Still read after a script operand, as before: fail closed on the next word.
        ("bash run.sh -c 'a'", ["a"]),
        ("bash -e run.sh", []),
        ("bash -o c run.sh", []),
        ("bash +c run.sh", []),
    ],
)
def test_payload_extraction(command, payloads):
    assert list(lifecycle_guard._iter_shell_command_payloads(command)) == payloads


# `--` and a lone `-` end a shell's options, so the next word is the operand even when it starts
# with `-` or `+` (bash and dash run `./-x.sh` for `bash -- -x.sh` / `sh - -x.sh`). Read as an
# unknown long option or an empty cluster instead, the end marker is skipped and the dashed word
# parsed as one more option cluster: the script (or `-c` payload) is then never read. A plain
# word after the marker (`bash -c -- 'x'`, `bash -e -- run.sh`) cannot tell the two reads apart.
@pytest.fixture
def dashed_scripts(tmp_path) -> str:
    (tmp_path / "-restart.sh").write_text(f"#!/bin/sh\n{LIFECYCLE}\n", encoding="utf-8")
    (tmp_path / "+restart.sh").write_text(f"#!/bin/sh\n{LIFECYCLE}\n", encoding="utf-8")
    (tmp_path / "-clean.sh").write_text("echo ok\n", encoding="utf-8")
    return str(tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "bash -- -restart.sh",
        "bash -e -- -restart.sh",
        "bash -e -- +restart.sh",
        "sh -- -restart.sh",
    ],
)
def test_double_dash_ends_options_before_a_dashed_script(dashed_scripts, command):
    assert scan_gateway_lifecycle(command, cwd=dashed_scripts) == (True, None)


@pytest.mark.parametrize(
    "command",
    [
        "bash - -restart.sh",
        "bash -e - -restart.sh",
        "bash -e - +restart.sh",
        "sh - -restart.sh",
    ],
)
def test_lone_dash_ends_options_before_a_dashed_script(dashed_scripts, command):
    assert scan_gateway_lifecycle(command, cwd=dashed_scripts) == (True, None)


@pytest.mark.parametrize("marker", ["--", "-"])
def test_dashed_clean_script_after_the_option_end_stays_allowed(dashed_scripts, marker):
    assert scan_gateway_lifecycle(f"bash -e {marker} -clean.sh", cwd=dashed_scripts) == (
        False, None)


@pytest.mark.parametrize(
    ("command", "payloads"),
    [
        ("bash -c -- '-a'", ["-a"]),
        ("bash -ec -- '+a'", ["+a"]),
        ("bash -c - '-a'", ["-a"]),
        ("bash -ec - '+a'", ["+a"]),
    ],
)
def test_option_end_marks_a_dashed_payload(command, payloads):
    assert list(lifecycle_guard._iter_shell_command_payloads(command)) == payloads


# An rc file before a clustered `-c` is scanned like the shell's script, but the `-c` payload is
# inline source: it is walked as commands, never opened as a file. Here the payload word `deploy`
# also names a file in the cwd that holds a lifecycle command; bash runs `deploy` from PATH (or
# fails), never `./deploy`, so the rc file is the only reference.
@pytest.fixture
def rcfile_dir(tmp_path) -> str:
    (tmp_path / "deploy").write_text(f"#!/bin/sh\n{LIFECYCLE}\n", encoding="utf-8")
    (tmp_path / "rc.sh").write_text("echo rc\n", encoding="utf-8")
    (tmp_path / "restart-rc.sh").write_text(f"{LIFECYCLE}\n", encoding="utf-8")
    return str(tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "bash --rcfile rc.sh -ec deploy",
        "bash --init-file rc.sh -lc deploy",
        "bash --rcfile rc.sh -c -e deploy",
    ],
)
def test_rcfile_scan_stops_at_a_clustered_c_payload(rcfile_dir, command):
    assert scan_gateway_lifecycle(command, cwd=rcfile_dir) == (False, None)
    references = set(lifecycle_guard._iter_referenced_shell_scripts(command, cwd=rcfile_dir))
    assert {path.name for path in references} == {"rc.sh"}


def test_restart_rcfile_before_a_clustered_c_payload_is_scanned(rcfile_dir):
    command = "bash --rcfile restart-rc.sh -ec true"
    assert scan_gateway_lifecycle(command, cwd=rcfile_dir) == (True, None)
    references = set(lifecycle_guard._iter_referenced_shell_scripts(command, cwd=rcfile_dir))
    assert {path.name for path in references} == {"restart-rc.sh"}
