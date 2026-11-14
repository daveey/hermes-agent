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
    # A nested comment's trailing backslash continues nothing: the newline still ends it.
    "echo ${x:-$(echo #c \\\n{cmd})}",
    "echo ${x:-$(true #\\\n{cmd})}",
    "echo ${x:-$(true #\\\\\n{cmd})}",
    "echo ${x:-${y:-$(true #\\\n{cmd})}}",
    "echo ${x:-$(echo ${y:-$(true #c \\\n{cmd})})}",
    "echo ${x:-$(true #c \\\r\n{cmd})}",
    # A case pattern's `)` does not close the substitution.
    "echo ${x:-$(case x in x) true\n{cmd};; esac)}",
    "echo ${x:-$(case x in x) true;; esac\n{cmd})}",
    "echo ${x:-$(case x in (x) true\n{cmd};; esac)}",
    "echo ${x:-$(case x in x) true #it's\n{cmd};; esac)}",
    "echo ${x:-$(case x in a|x) true\n{cmd};; esac)}",
    "echo ${x:-$(case x in\nx) true\n{cmd};; esac)}",
    "echo ${x:-$(true; case x in x) true\n{cmd};; esac)}",
    "echo ${x:-$(echo $(case x in x) true\n{cmd};; esac))}",
    "echo ${x:-$(case x in x) case y in y) true\n{cmd};; esac;; esac)}",
    "echo ${x:-$(case x in x) true;; esac; true\n{cmd})}",
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


# In a case pattern list only a leading `esac` is reserved: a pattern spelled `case` (or `in`,
# `esac` after `(`/`|`) opens nothing, so the real `)` still closes the substitution and the
# command after a double-quoted frame stays visible.
CASE_PATTERN_FRAMES = [
    'echo "${x:-$(case x in (case) true;; esac)}"; {cmd}',
    'echo "${x:-$(case x in case) true;; esac)}"; {cmd}',
    'echo "${x:-$(case x in a|case) true;; esac)}"; {cmd}',
    'echo "${x:-$(case x in x) true;; case) true;; esac)}"; {cmd}',
    'echo "${x:-$(case x in x) true;& case) true;; esac)}"; {cmd}',
    'echo "${x:-$(case x in x) true;;& case) true;; esac)}"; {cmd}',
    'echo "${x:-$(case x in\ncase) true;; esac)}"; {cmd}',
    'echo "${x:-$(case x in (esac) true;; esac)}"; {cmd}',
    'echo "${x:-$(case x in a|esac) true;; esac)}"; {cmd}',
    'echo "${x:-$(case x in (in) true;; esac)}"; {cmd}',
    'echo "${x:-$(case case in (case) true;; esac)}"; {cmd}',
    'echo ${x:-"$(case x in (case) true;; esac)"}; {cmd}',
    'x="${y:-$(case x in (case) true;; esac)}"; {cmd}',
    'echo "${x:-$(case x in (case) true;; esac)}" && {cmd}',
    'echo "${x:-$(case x in x) case y in (case) true;; esac;; esac)}"; {cmd}',
]


# A here-document body is data: a body line spelled `case x in` (or `;;`, `x)`) moves no case
# state, so the `)` after the body still closes the substitution and the next command is read.
HEREDOC_FRAMES = [
    'echo "${x:-$(cat <<E\ncase x in\nE\n)}"; {cmd}',
    'echo "${x:-$(cat <<\'E\'\ncase x in\nE\n)}"; {cmd}',
    'echo "${x:-$(cat <<"E"\ncase x in\nE\n)}"; {cmd}',
    'echo "${x:-$(cat <<-E\n\tcase x in\n\tE\n)}"; {cmd}',
    'echo "${x:-$(cat << E\ncase x in\nE\n)}"; {cmd}',
    'echo ${x:-"$(cat <<E\ncase x in\nE\n)"}; {cmd}',
    'x="${y:-$(cat <<E\ncase x in\nE\n)}"; {cmd}',
    'echo "${x:-$(cat <<E\ncase x in\nE\n)}" && {cmd}',
    'echo "${x:-$(cat <<A <<B\na\nA\ncase x in\nB\n)}"; {cmd}',
    'echo "${x:-$(cat <<E; case x in x) true;; esac\ncase x in\nE\n)}"; {cmd}',
    'echo "${x:-$(case x in x) cat <<E\n;;\nx)\nE\n;; esac)}"; {cmd}',
    # Nor does a paren, quote or comment in the body move the frame.
    'echo "${x:-$(cat <<E\n(\nE\n)}"; {cmd}',
    'echo "${x:-$(cat <<E\nit\'s\nE\n)}"; {cmd}',
    'echo "${x:-$(cat <<\'E\'\nsay "hi\nE\n)}"; {cmd}',
    'echo "${x:-$(cat <<E\n`\nE\n)}"; {cmd}',
    # Inside `$(...)` a body also ends at a delimiter line followed by `)`.
    'echo "${x:-$(cat <<E\ncase x in\nE)}"; {cmd}',
    'echo "${x:-$(cat <<E\n(\nE )}"; {cmd}',
    'echo "${x:-$(cat <<-E\ncase x in\n\tE)}"; {cmd}',
    # `<<<` and arithmetic `<<` open no here-document; case tracking stays on.
    'echo "${x:-$(cat <<<y; case x in (case) true;; esac)}"; {cmd}',
    'echo "${x:-$(echo $((1<<2)); case x in (case) true;; esac)}"; {cmd}',
    'echo "${x:-$(((1<<2)); case x in (case) true;; esac)}"; {cmd}',
]


@pytest.mark.parametrize("frame", HEREDOC_FRAMES)
@pytest.mark.parametrize("dangerous", [True, False], ids=["RUN", "clean"])
def test_heredoc_body_does_not_open_case(tmp_path, frame, dangerous):
    helpers._tree(tmp_path)
    command = frame.replace("{cmd}", "bash ./r.sh" if dangerous else "python3 ./clean.py")
    assert helpers._bash_runs(tmp_path, command) is dangerous
    refused, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert refused is dangerous, reason


@pytest.mark.parametrize("frame", CASE_PATTERN_FRAMES)
@pytest.mark.parametrize("dangerous", [True, False], ids=["RUN", "clean"])
def test_case_word_as_pattern_does_not_hide_following_command(tmp_path, frame, dangerous):
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
