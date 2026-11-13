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
