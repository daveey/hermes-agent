"""Real Bash regressions: a ``let`` command word hidden by quotes or escapes.

Bash removes quotes before it looks the command word up, so ``\\let``, ``'let'``,
``l\\et``, ``$'\\x6cet'`` and friends all run the ``let`` builtin, whose operands are
arithmetic; an array subscript in them runs its command substitutions
(t_01a1db24f88f8d5b). The scanner's bare ``let`` token needed the literal word.
"""

import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.shell_arith_sink import substitution_in_arithmetic
from tools.shell_heredoc import output_reaches_shell
from tools.shell_let_word import hidden_let_end

# NUL truncates only its ANSI-C piece, not subsequent quoted/literal pieces.
REVIEW_SINKS = [
    r"$'let\c`ignored' 'a[$(SCRIPT)]=1'",
    r"$'le\c`ignored't 'a[$(SCRIPT)]=1'",
    r"$'le\c`ignored\'still ignored't 'a[$(SCRIPT)]=1'",
    r"$'let\0ignored' 'a[$(SCRIPT)]=1'",
    r"$'le\x00ignored't 'a[$(SCRIPT)]=1'",
    r"$'le\u0000ignored't 'a[$(SCRIPT)]=1'",
    r"$'le\U00000000ignored't 'a[$(SCRIPT)]=1'",
    r"$'le\c@ignored't 'a[$(SCRIPT)]=1'",
    r"$'le\000ignored\'still ignored't 'a[$(SCRIPT)]=1'",
    "$'le\\0" + "ignored" * 40 + "'t 'a[$(SCRIPT)]=1'",
    '"l' + "\\\n" * 21 + 'et" \'a[$(SCRIPT)]=1\'',
]

# SCRIPT is an executable payload that touches a marker and prints 0.
SINKS = REVIEW_SINKS + [
    r"\let 'a[$(SCRIPT)]=1'",
    r"'let' 'a[$(SCRIPT)]=1'",
    r'"let" "a[$(SCRIPT)]=1"',
    r"l\et 'a[$(SCRIPT)]=1'",
    r"le\t 'a[$(SCRIPT)]=1'",
    r"l'e't 'a[$(SCRIPT)]=1'",
    r'l"et" a[\$\(SCRIPT\)]=1',
    r"''let 'a[$(SCRIPT)]=1'",
    r"let'' 'a[$(SCRIPT)]=1'",
    r"$'let' 'a[$(SCRIPT)]=1'",
    r"$'\x6cet' 'a[$(SCRIPT)]=1'",
    r"$'\154et' 'a[$(SCRIPT)]=1'",
    r"$'\u006cet' 'a[$(SCRIPT)]=1'",
    r"$'\554et' 'a[$(SCRIPT)]=1'",
    r"$'l\x65t' 'a[$(SCRIPT)]=1'",
    r'$"let" "a[$(SCRIPT)]=1"',
    "l\\\net 'a[$(SCRIPT)]=1'",
    "\"l\\\net\" 'a[$(SCRIPT)]=1'",
    r"\let x=1 'a[$(SCRIPT)]=1'",
    r'\let "a[$(SCRIPT)]=1"',
    r"x=1; \let 'a[$(SCRIPT)]=1'",
    r"true && 'let' 'a[$(SCRIPT)]=1'",
    r"if \let 'a[$(SCRIPT)]=1'; then :; fi",
    r"( \let 'a[$(SCRIPT)]=1' )",
    r"builtin 'let' 'a[$(SCRIPT)]=1'",
    r"command \let 'a[$(SCRIPT)]=1'",
    r"FOO=1 \let 'a[$(SCRIPT)]=1'",
    "\\let\t'a[$(SCRIPT)]=1'",
    # Inside `$(( ))` / `${a[ ]}` a `let`-spelling word is arithmetic text, not a `let`
    # command: its later apostrophes keep the `$(( ))` rules, where `$(` runs.
    r"x=$(( \let '$(SCRIPT)' )) 2>/dev/null",
    r"x=$(( 'let' + '$(SCRIPT)' )) 2>/dev/null",
    r"a=(1); echo ${a[ \let '$(SCRIPT)' ]} 2>/dev/null",
]

# The substitution runs as an ordinary one inside "…", so its output is printed, never
# evaluated: a quoted `\let` there is text, not a command word.
PRINTED = [
    r"""echo "\let 'a[$(SCRIPT)]=1'" """,
    r"""echo "x \let 'a[$(SCRIPT)]=1'" """,
    r"""echo "x 'let' 'a[$(SCRIPT)]=1'" """,
]

# Bash runs none of these: no word is `let` after quote removal, or none of its operands
# holds a subscript substitution.
DATA = [
    r"$'let\c`ignored'x 'a[$(SCRIPT)]=1' 2>/dev/null",
    r"$'le\c`ignored' 'a[$(SCRIPT)]=1' 2>/dev/null",
    r"$'let\cAignored' 'a[$(SCRIPT)]=1' 2>/dev/null",
    r"$'let\caignored' 'a[$(SCRIPT)]=1' 2>/dev/null",
    r"$'let\0ignored'x 'a[$(SCRIPT)]=1' 2>/dev/null",
    r"$'lex\0let' 'a[$(SCRIPT)]=1' 2>/dev/null",
    r"$'letx\0' 'a[$(SCRIPT)]=1' 2>/dev/null",
    r"$'le\0ignored' 'a[$(SCRIPT)]=1' 2>/dev/null",
    r"\letter 'a[$(SCRIPT)]=1' 2>/dev/null",
    r"'let'x 'a[$(SCRIPT)]=1' 2>/dev/null",
    r"'let x' 'a[$(SCRIPT)]=1' 2>/dev/null",
    r"x='let' 'a[$(SCRIPT)]=1' 2>/dev/null",
    r"\let 'a[1]=1'; echo 'a[$(SCRIPT)]'",
    r"'let' 'a[1]=1' && echo '$(SCRIPT)'",
    r"\let '$(SCRIPT)' 2>/dev/null",
    r"\let 'a[$(date +%s)]=1'",
]


def _run(tmp_path, shape):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash required for execution proof")
    marker = tmp_path / "ran"
    script = tmp_path / "payload.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\nprintf 0\n", encoding="utf-8")
    script.chmod(0o755)
    command = shape.replace("SCRIPT", str(script))
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", command], cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
        encoding="utf-8", timeout=10, check=False,
    )
    return command, marker.exists(), result


# Bash 3.2 preserves Unicode escapes literally and rejects this parameter subscript
# before evaluating its quoted substitution. These remain scanner sinks: the guard
# must conservatively cover newer Bash even when the local shell treats them as data.
BASH32_DATA = {
    r"$'le\u0000ignored't 'a[$(SCRIPT)]=1'": (127, "command not found"),
    r"$'le\U00000000ignored't 'a[$(SCRIPT)]=1'": (127, "command not found"),
    r"$'\u006cet' 'a[$(SCRIPT)]=1'": (127, "command not found"),
    r"a=(1); echo ${a[ \let '$(SCRIPT)' ]} 2>/dev/null": (1, "arithmetic operator"),
}


@pytest.fixture(scope="module")
def bash_version():
    bash = shutil.which("bash")
    assert bash is not None, "Bash required for execution proof"
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-c",
         'printf "%s.%s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"'],
        env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
        encoding="utf-8", timeout=10, check=True,
    )
    return result.stdout


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("shape", SINKS)
def test_hidden_let_subscripts_are_sinks(tmp_path, shape, bash_version):
    command, ran, result = _run(tmp_path, shape)
    if bash_version == "3.2" and shape in BASH32_DATA:
        status, diagnostic = BASH32_DATA[shape]
        assert not ran, (result.returncode, result.stderr)
        assert result.returncode == status
        assert diagnostic in result.stderr
    else:
        assert ran, (result.returncode, result.stderr)
    assert substitution_in_arithmetic(command, date_exempt=True)
    assert output_reaches_shell(command)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("shape", DATA)
def test_words_that_are_not_let_stay_data(tmp_path, shape):
    command, ran, result = _run(tmp_path, shape)
    assert not ran, (result.returncode, result.stderr)
    assert not substitution_in_arithmetic(command, date_exempt=True)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("shape", PRINTED)
def test_double_quoted_let_text_is_printed_not_evaluated(tmp_path, shape):
    command, ran, result = _run(tmp_path, shape)
    assert ran and "a[0]=1" in result.stdout, (result.stdout, result.stderr)
    assert not substitution_in_arithmetic(command, date_exempt=True)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("shape", SINKS[:len(REVIEW_SINKS) + 6])
def test_lifecycle_guard_scans_the_hidden_let_payload(tmp_path, shape):
    script = tmp_path / "payload.sh"
    script.write_text("# padding\n" * 150000, encoding="utf-8")  # past the scan cap
    script.chmod(0o755)
    unsafe, refusal = scan_gateway_lifecycle(
        shape.replace("SCRIPT", str(script)), cwd=str(tmp_path))
    assert unsafe, refusal


@pytest.mark.parametrize(("command", "token", "start", "end"), [
    (r"\let x", "\\l", 0, 4),
    (r"'let' x", "'", 0, 5),
    (r"l\et x", "\\e", 1, 4),
    (r"let'' x", "'", 3, 5),
    ("$'\\x6c\\x65t' x", "$'", 0, 12),
    ('$"let" x', '"', 1, 6),
    ("l\\\net x", "\\\n", 1, 5),
    (r"echo;\let x", "\\l", 5, 9),
])
def test_hidden_let_end_spans_the_whole_word(command, token, start, end):
    assert hidden_let_end(command, token, start) == end


@pytest.mark.parametrize(("command", "token", "start"), [
    (r"\let", "\\l", 0),               # no operand follows
    (r"\let;x", "\\l", 0),             # a metacharacter ends the word: no blank
    (r"x\let y", "\\l", 1),            # inside another word
    (r"='let' y", "'", 1),             # an assignment value
    (r"\letx y", "\\l", 0),
    (r"'lets' y", "'", 0),
    ('"l$e"t y', '"', 0),              # an expansion is not a static command word
    (r'"l\et" y', '"', 0),             # in "…" a backslash before a letter stays
    (r'"\let" y', '"', 0),
    (r"$'l\et' y", "$'", 0),           # \e is ESC
    (r"$'\l'et y", "$'", 0),           # an unknown escape keeps its backslash
    ("$'\\cl'et y", "$'", 0),          # a control character, not a letter
    ("'" + "l" * 60 + "' y", "'", 0),  # overlong quoted spans stop early
    ("$'\\U110000let' y", "$'", 0),    # out-of-range code point
    ("$'\\", "$'", 0),                 # an unclosed ANSI-C string ending in a backslash
    (r"\; x", "\\;", 0),
    ("x y", "x", 0),                   # not a quote or escape token
])
def test_hidden_let_end_rejects_other_words(command, token, start):
    assert hidden_let_end(command, token, start) is None
