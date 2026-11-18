"""Redirection words where the command word is read: bash's ``{NAME}`` descriptor-variable forms
and ``>&``/``<&``/``>|`` (card t_01a1d5f876488f96).

``{fd}>/dev/null bash ./r.sh`` allocates a descriptor into ``$fd`` and runs bash; the guard read
``{fd}`` as the command word and allowed it. ``2>&1 bash ./r.sh`` was cut at the ``&`` into
``2>`` and a command ``1``. The shape corpus (``heredoc_shape_corpus``, ``s_position ==
"redirect"``) runs each shape in real bash under every wrapper and nesting form.
"""

from __future__ import annotations

import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from cron.lifecycle_guard_groups import split_glued_redirections

RUN = [
    "{fd}>/dev/null bash ./r.sh",
    "{fd}>&2 bash ./r.sh",
    "{fd}>& 2 bash ./r.sh",
    "{fd}> /dev/null bash ./r.sh",
    "{fd}<>./log bash ./r.sh",
    "{fd}>>./log bash ./r.sh",
    "{fd}>|./log bash ./r.sh",
    "{fd}<<<x bash ./r.sh",
    "{fd}<&0 bash ./r.sh",
    "{a}>/dev/null {b}>&2 bash ./r.sh",
    # Array-element descriptor variables (bash 5.2 stores the fd in the element).
    "{a[1]}>/dev/null bash ./r.sh",
    "{a[1]}>&2 bash ./r.sh",
    "bash {a[1]}>/dev/null ./r.sh",
    "env {m[k]}>&2 bash ./r.sh",
    "declare -A m; {m[k]}>&2 bash ./r.sh",
    "{a[b[1]]}>/dev/null bash ./r.sh",
    "{a[$((1+1))]}>/dev/null bash ./r.sh",
    "{a[\"x\"]}>/dev/null bash ./r.sh",
    # A subscript the tokenizer cuts (`$((`, `$(`, backticks) or a quoted space in it.
    "declare -A m; {m[\"k k\"]}>/dev/null bash ./r.sh",
    "{a[$(echo 1)]}>/dev/null bash ./r.sh",
    "{a[`echo 1`]}>&2 bash ./r.sh",
    "env {a[$((1+1))]}>&2 bash ./r.sh",
    "{a[$(bash ./r.sh)]}>/dev/null echo hi",
    "bash -s {a[`echo 1`]}>/dev/null < ./r.sh",
    # A `{NAME[` head cut by a space with no `]}>` tail is an ordinary word: its command keeps
    # the stdin redirection after it (review t_01a1d6855e13d1d2).
    "bash -s {a[x < ./r.sh",
    "bash -s {a[x <./r.sh",
    "bash -s {a[x 0<./r.sh",
    "bash -s -- {a[x < ./r.sh",
    "sudo bash -s {a[x < ./r.sh",
    "sh -s {a[x < ./r.sh",
    "bash -s {m[k < ./r.sh",
    "bash -s {a[x 1 < ./r.sh",
    "bash -s {a[x 2>/dev/null < ./r.sh",
    "bash -s {a[1 2]} < ./r.sh",
    "bash -s {a[1 2]}>/dev/null < ./r.sh",
    "cat ./r.sh | bash -s {a[`echo 1`]}>/dev/null",
    "{fd}>/dev/null bash < ./r.sh",
    "env {fd}>/dev/null bash ./r.sh",
    "timeout 5 {fd}>&2 bash ./r.sh",
    "{fd}>/dev/null sudo bash ./r.sh",
    "bash {fd}>/dev/null ./r.sh",
    "bash {fd}>&2 ./r.sh",
    "f() { {fd}>&2 bash ./r.sh; }; f",
    "( {fd}>&2 bash ./r.sh )",
    "echo x; {fd}>&2 bash ./r.sh",
    "2>&1 bash ./r.sh",
    ">&2 bash ./r.sh",
    "2>|./log bash ./r.sh",
    "bash 2>&1 ./r.sh",
    "0<&- bash ./r.sh",
]
ALLOWED = [
    "{fd}>/dev/null python3 ./clean.py",
    "{fd}>&2 python3 ./clean.py",
    "python3 {fd}>/dev/null ./clean.py",
    "{fd}>/dev/null echo hi",
    "{a[1]}>/dev/null python3 ./clean.py",
    "env {m[k]}>&2 python3 ./clean.py",
    "{a[$((1+1))]}>/dev/null python3 ./clean.py",
    "{a[`echo 1`]}>/dev/null python3 ./clean.py",
    "echo {fd}>/dev/null",
    "python3 ./clean.py 2>&1 | tee ./log",
    # Not a `{NAME}` redirection: bash runs the word `{1a}` / `q{fd}` with a plain `>` redirect.
    "{1a}>/dev/null echo bash ./r.sh",
    "echo q{fd}>/dev/null bash ./r.sh",
    "{a[]}>/dev/null echo bash ./r.sh",
    "echo {a[x bash ./r.sh",
    # A quoted or escaped `>` is text, so the `&` after it is still a control operator.
    "echo '2>'& echo bash ./r.sh",
    "echo \\>& echo bash ./r.sh",
]


def _tree(tmp_path):
    (tmp_path / "r.sh").write_text('hermes gateway "restart"\n')
    (tmp_path / "clean.py").write_text("print(1)\n")
    (tmp_path / "bin").mkdir()
    for name, body in (("hermes", 'printf called > "$MARKER"'), ("sudo", 'exec "$@"')):
        tool = tmp_path / "bin" / name
        tool.write_text(f"#!/bin/sh\n{body}\n")
        tool.chmod(0o755)


def _bash_runs(tmp_path, command: str) -> bool:
    from tests.cron.bash_execution import require_modern_system_bash

    require_modern_system_bash()
    marker = tmp_path / "marker"
    marker.unlink(missing_ok=True)
    subprocess.run(["/bin/bash"], input=command + "\n", text=True, capture_output=True,
                   timeout=20, check=False, cwd=tmp_path,
                   env={"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "MARKER": str(marker),
                        "HOME": str(tmp_path)})
    return marker.exists()


@pytest.mark.parametrize("command", RUN)
def test_redirection_before_or_among_command_words_still_refused(tmp_path, command):
    _tree(tmp_path)
    assert _bash_runs(tmp_path, command), "bash must run the restart, or the row proves nothing"
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0] is True


@pytest.mark.parametrize("command", ALLOWED)
def test_redirection_words_on_clean_commands_stay_allowed(tmp_path, command):
    _tree(tmp_path)
    assert not _bash_runs(tmp_path, command)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize(("line", "split"), [
    ("{fd}>x bash", "{fd}>x bash"),
    ("{fd}>&2 bash", "{fd}>\\&2 bash"),
    ("2>&1 bash", "2>\\&1 bash"),
    ("x <&0 y", "x <\\&0 y"),
    ("x >|f y", "x >\\|f y"),
    ("q{fd}>x", "q{fd} >x"),
    ('"{fd}">x', '"{fd}" >x'),
    ("{1a}>x", "{1a} >x"),
    ("{a[1]}>x bash", "{a[1]}>x bash"),
    ("{a[b[1]]}>&2 bash", "{a[b[1]]}>\\&2 bash"),
    ('{a["x"]}>x', '{a["x"]}>x'),
    ("{a[1]x}>x", "{a[1]x} >x"),
    ("{a[]}>x", "{_}>x ; {a[]}"),  # bash runs `{a[]}`; the guard fails closed
    ('{m["k k"]}>x', '{m["k k"]}>x'),
    # A cut subscript glued to its operator becomes `{_}`; its text becomes a command of its own
    # at the line's end (before a comment), or its start when the line ends in an operator.
    ("{a[$((1+1))]}>x bash", "{a[$((1+1)){_}>x bash ; ]}"),
    ("{a[`echo 1`]}>&2 bash", "{_}>\\&2 bash ; {a[`echo 1`]}"),
    ("bash -s {a[`e 1`]}>x < f; y", "bash -s {_}>x < f; y ; {a[`e 1`]}"),
    ("{a[`e 1`]}>x bash # c", "{_}>x bash ; {a[`e 1`]} # c"),
    ('echo "$(x)"]}>y', 'echo {_}>y ; "$(x)"]}'),
    ("{a[`e 1`]}>x cat f |", "{a[`e 1`]} ; {_}>x cat f |"),
    ("cat f | {a[`e 1`]}>x bash | tee g", "cat f | {_}>x bash | tee g ; {a[`e 1`]}"),
    # A head with no glued `]}` tail is an ordinary word.
    ("echo {a[x y", "echo {a[x y"),
    ("bash -s {a[x < f", "bash -s {a[x < f"),
    ("bash -s {a[1 2]} < f", "bash -s {a[1 2]} < f"),
    ("a && b || c", "a && b || c"),
    ("a <(b) | c", "a <(b) | c"),
    ("echo '>&' x", "echo '>&' x"),
    ("echo \\>& x", "echo \\>& x"),
    ("a > & b", "a > & b"),
])
def test_split_glued_redirections_keeps_fd_variables_and_operator_pairs(line, split):
    assert split_glued_redirections(line) == split
