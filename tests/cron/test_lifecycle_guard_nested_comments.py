"""Nested Bash lexical contexts must not hide a later referenced script."""

import pytest

from cron.lifecycle_guard import _shlex_tokens, scan_gateway_lifecycle
from tests.cron import test_lifecycle_guard_comment_words as helpers

PREFIXES = [
    "echo $'a>&b' 2>&1",
    r"echo $'a>\x26b' 2>&1",
    r"echo $'it\'s>&b' 2>&1",
    'echo ${x:-"$(echo $\'it\\\'s\' #say "x\n)"}',
    'echo "${x:-$(echo $\'it\\\'s\' #say "x\n)}"',
    'echo ${x:-"$(echo #say "x\n)"}',
    'echo "${x:-$(echo #say "x\n)}"',
    'echo ${x:-"$(echo #} "\n)"}',
    'echo ${x:-$(echo #say "x\n)}',
    "echo ${x:-$(echo #it's\n)}",
    'echo ${x:-$(echo #} "\n)}',
    'echo ${x:-"$(echo "$(echo #say "x\n)")"}',
    'echo ${x:-"$(echo ${y:-$(echo #say "x\n)})"}',
    "x='a b'; echo ${x// #/y}",
    "echo ${x:-a #b}",
    "echo `echo #x`",
    r"echo $'it\'s'",
    "echo ${x:-${y:-a #b}}",
    "echo ${x:-'}' #b}",
    r"echo ${x:-a\} #b}",
    'echo "${x:-a #b}"',
    "echo `echo #it's`",
    "echo ${x:-`echo #it's`}",
    "echo ${x:-${y:-`echo #it's`}}",
    'echo ${x:-"`echo #it\'s`"}',
    'echo "${x:-`echo #it\'s`}"',
    'echo "`echo #x`"',
    'echo ${x:-"`echo #say "x`"}',
    'echo ${x:-${y:-"`echo #say "x`"}}',
    'echo ${x:-"${y:-`echo #say "x`}"}',
    'echo ${x:-"before `echo #say "x` after"}',
    'echo ${x:-"escaped \\" before `echo #say "x`"}',
    r"echo $'it\'s'#word",
    r"echo $'it\'s' " + "# it's\ntrue",
    r"echo ${x:-$'it\'s'}",
]


@pytest.mark.parametrize("word, literal", [
    ("$'a>&b'", "a>&b"),
    (r"$'a>\x26b'", "a>&b"),
    (r"$'it\'s>&b'", "it's>&b"),
])
def test_ansi_ampersands_preserve_words_and_descriptor_redirects(word, literal):
    assert _shlex_tokens(f"echo {word} 2>&1") == ["echo", literal, "2>&1"]


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("dangerous", [True, False], ids=["RUN", "clean"])
def test_nested_context_does_not_hide_following_command(tmp_path, prefix, dangerous):
    helpers._tree(tmp_path)
    command = prefix + ("; bash ./r.sh" if dangerous else "; python3 ./clean.py")
    assert helpers._bash_runs(tmp_path, command) is dangerous
    refused, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert refused is dangerous, reason


# A newline inside a `$(...)` nested in a parameter word separates commands there, so the
# command after it runs even though the `${...}` word is framed as one opaque span.
NEWLINE_FRAMES = [
    "echo ${x:-$(true\n{cmd})}",
    "echo ${x:-$(echo #c\n{cmd})}",
    "echo ${x:-$(echo #it's\n{cmd})}",
    "echo ${x:-${y:-$(true\n{cmd})}}",
    "echo ${x:-$(echo ${y:-$(true\n{cmd})})}",
    "echo $(echo ${x:-$(true\n{cmd})})",
    "echo ${x:-$(true\ntrue\n{cmd})}",
    # A quoted newline inside the nested substitution is data, not a separator.
    "echo ${x:-$(echo \"a\nb\")}\n{cmd}",
    "echo ${x:-$(echo 'a\nb')}; {cmd}",
]


@pytest.mark.parametrize("frame", NEWLINE_FRAMES)
@pytest.mark.parametrize("dangerous", [True, False], ids=["RUN", "clean"])
def test_newline_in_nested_substitution_separates_commands(tmp_path, frame, dangerous):
    helpers._tree(tmp_path)
    command = frame.replace("{cmd}", "bash ./r.sh" if dangerous else "python3 ./clean.py")
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
