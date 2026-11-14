"""Nested Bash lexical contexts must not hide a later referenced script."""

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tests.cron import test_lifecycle_guard_comment_words as helpers

PREFIXES = [
    "x='a b'; echo ${x// #/y}",
    "echo ${x:-a #b}",
    "echo `echo #x`",
    r"echo $'it\'s'",
    "echo ${x:-${y:-a #b}}",
    "echo ${x:-'}' #b}",
    r"echo ${x:-a\} #b}",
    'echo "${x:-a #b}"',
    "echo `echo #it's`",
    'echo "`echo #x`"',
    r"echo $'it\'s'#word",
    r"echo $'it\'s' " + "# it's\ntrue",
    r"echo ${x:-$'it\'s'}",
]


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("dangerous", [True, False], ids=["RUN", "clean"])
def test_nested_context_does_not_hide_following_command(tmp_path, prefix, dangerous):
    helpers._tree(tmp_path)
    command = prefix + ("; bash ./r.sh" if dangerous else "; python3 ./clean.py")
    assert helpers._bash_runs(tmp_path, command) is dangerous
    refused, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert refused is dangerous, reason


@pytest.mark.parametrize("escape", [r"\0", r"\x00"])
@pytest.mark.parametrize("dangerous", [True, False], ids=["RUN", "clean"])
def test_ansi_nul_truncates_only_quoted_span(tmp_path, escape, dangerous):
    helpers._tree(tmp_path)
    script = "./r" if dangerous else "./clean"
    suffix = ".sh" if dangerous else ".py"
    interpreter = "bash" if dangerous else "python3"
    command = f"{interpreter} $'{script}{escape}ignored'{suffix}"
    assert helpers._bash_runs(tmp_path, command) is dangerous
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0] is dangerous
