"""Shell reserved words before a command do not hide what it runs.

The referenced-script walk read each segment at its first token and at the wrapper-peeled one, so
inside the gateway `./x.sh` (x.sh restarting the gateway) was refused while `time ./x.sh`,
`! ./x.sh`, `if true; then ./x.sh; fi`, `{ ./x.sh; }` and `while false; do ./x.sh; done` ran:
the walk saw `time`/`!`/`then`/`{`/`do` as the command and never opened the script. The same gap
let interpreter runs (`time python3 x.py`), `sh -c` payloads (`then bash -c './x.sh'`) and
`launchctl submit` behind a keyword through.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from cron import lifecycle_guard
from cron.lifecycle_guard import contains_launchctl_submit_command, scan_gateway_lifecycle

# Assembled so this file's own text never carries the literal command.
LIFECYCLE = " ".join(["hermes", "gateway", "re" + "start"])


def _executable(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return shlex.quote(str(path))


@pytest.fixture
def script(tmp_path) -> str:
    return _executable(tmp_path / "x.sh", f"#!/bin/sh\n{LIFECYCLE}\n")


@pytest.fixture
def runner(tmp_path) -> str:
    return _executable(tmp_path / "runner.py", f"import os\nos.system({LIFECYCLE!r})\n")


_KEYWORD_TEMPLATES = [
    "time {p}",
    "time -p {p}",
    "time -p -- {p}",
    "! {p}",
    "! time {p}",
    "if true; then {p}; fi",
    "if {p}; then :; fi",
    "if false; then :; elif {p}; then :; fi",
    "if false; then :; else {p}; fi",
    "while false; do {p}; done",
    "while {p}; do :; done",
    "until {p}; do :; done",
    "for i in 1; do {p}; done",
    "{{ {p}; }}",
    "{{ {p} arg; }} > /dev/null",
    "if true; then {{ ! {p}; }}; fi",
    # A keyword, then the env assignments and wrappers the walk already peels.
    "time FOO=1 {p}",
    "time nohup {p}",
    "! sudo -u root {p}",
    # A keyword before a shell or a sourced script.
    "time bash {p}",
    "then sh -o pipefail {p}",
    "time . {p}",
    "if true; then source {p}; fi",
    # coproc runs its command in the background.
    "coproc {p}",
    "coproc {{ {p}; }}",
    "coproc NAME {{ {p}; }}",
    # The external time(1), also reached through `command`.
    "/usr/bin/time {p}",
    "/usr/bin/time -f %e -o /dev/null {p}",
    "command time -p {p}",
]


@pytest.mark.parametrize("template", _KEYWORD_TEMPLATES)
def test_script_behind_a_reserved_word_is_scanned(script, template):
    assert scan_gateway_lifecycle(template.format(p=script)) == (True, None)


@pytest.mark.parametrize("template", [
    "time python3 {p}",
    "if true; then python3 -u {p}; fi",
    "! /usr/bin/env python3 {p}",
    "while false; do timeout 5 python3 {p}; done",
    "{{ python3 {p}; }}",
    "/usr/bin/time python3 {p}",
])
def test_interpreter_run_behind_a_reserved_word_is_scanned(runner, template):
    assert scan_gateway_lifecycle(template.format(p=runner)) == (True, None)


@pytest.mark.parametrize("template", [
    "time bash -c {q}",
    "if true; then sh -c {q}; fi",
    "! nohup bash -c {q}",
    "time env -S {q}",
    "then su -c {q}",
    "/usr/bin/time bash -c {q}",
])
def test_shell_payload_behind_a_reserved_word_is_scanned(script, template):
    assert scan_gateway_lifecycle(template.format(q=shlex.quote(script))) == (True, None)


@pytest.mark.parametrize("command", [
    "time launchctl submit -l x -- /bin/true",
    "if true; then launchctl submit -l x -- /bin/true; fi",
    "! launchctl bootstrap gui/501 /tmp/x.plist",
    "{ launchctl submit -l x -- /bin/true; }",
    "/usr/bin/time launchctl submit -l x -- /bin/true",
])
def test_launchctl_submit_behind_a_reserved_word_is_detected(command):
    assert contains_launchctl_submit_command(command)
    assert scan_gateway_lifecycle(command)[0] is True


@pytest.mark.parametrize("template", [
    "time {p}",
    "! {p}",
    "if true; then {p}; fi",
    "{{ {p}; }}",
    "while false; do {p}; done",
])
def test_unscannable_script_behind_a_reserved_word_fails_closed(tmp_path, monkeypatch, template):
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINES", 50)
    big = _executable(tmp_path / "big.sh", "#!/bin/sh\n" + "echo ok\n" * 100)

    unsafe, refusal = scan_gateway_lifecycle(template.format(p=big))

    assert unsafe is True
    assert refusal is not None
    assert "big.sh" in refusal


@pytest.mark.parametrize("template", [
    # Mentions stay mentions: a keyword spelled as an argument is not a command prefix.
    "echo time {p}",
    "echo if {p}",
    "printf '%s\\n' '!' {p}",
    "time echo {p}",
    "if true; then echo {p}; fi",
    "{{ cat {p}; }}",
])
def test_reserved_word_does_not_turn_a_mention_into_a_run(script, template):
    assert scan_gateway_lifecycle(template.format(p=script)) == (False, None)


def test_keyword_peel_is_additive(tmp_path):
    """A script literally named like a keyword is still read at the original token."""
    local = _executable(tmp_path / "time", f"#!/bin/sh\n{LIFECYCLE}\n")

    assert scan_gateway_lifecycle(f"{local} arg") == (True, None)
