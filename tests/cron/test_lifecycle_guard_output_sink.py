"""A data consumer whose OUTPUT a shell runs is not data (review 5888336995 nit 3, PR #1671).

``python3 - <<'PY' | bash`` printing ``bash /x/huge.sh`` runs that script, so the walk must treat
the path as executed (an oversized script fails closed) instead of a mention in a masked body or
an interpreter string literal. The shape corpus (``heredoc_shape_corpus``, ``s_position ==
"sink"``) crosses every sink with every wrapper and nesting form under real bash.
"""

import shutil
import subprocess
import time

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.shell_heredoc import output_reaches_shell, strip_inert_heredoc_bodies

_PRINT = "print('bash {big}')\n"
# The reviewer's two repros verbatim (paths moved under tmp_path), then the other sinks the card
# names.
_SINK_SHAPES = {
    "review-pipe": "python3 - <<'PY' | bash\n" + _PRINT + "PY\n",
    "review-function": "f() { python3 - <<'PY'\n" + _PRINT + "PY\n}; f | bash\n",
    "function-newline": "f() { python3 - <<'PY'\n" + _PRINT + "PY\n}\nf | bash\n",
    "pipe-sh-s": "python3 - <<'PY' | sh -s\n" + _PRINT + "PY\n",
    "pipe-sudo": "python3 - <<'PY' | sudo bash\n" + _PRINT + "PY\n",
    "pipe-ssh": "python3 - <<'PY' | ssh h bash\n" + _PRINT + "PY\n",
    "eval": "eval \"$(python3 - <<'PY'\n" + _PRINT + "PY\n)\"\n",
    "eval-cat": "eval \"$( cat <<'PY'\nbash {big}\nPY\n )\"\n",
    "procsub": "bash <(python3 - <<'PY'\n" + _PRINT + "PY\n)\n",
    "dot-procsub": ". <(python3 - <<'PY'\n" + _PRINT + "PY\n)\n",
    "subshell": "(\npython3 - <<'PY'\n" + _PRINT + "PY\n) | bash\n",
    "group-cat": "{\ncat <<'PY'\nbash {big}\nPY\n} | bash\n",
    "variable-eval": "o=$(\npython3 - <<'PY'\n" + _PRINT + "PY\n)\neval \"$o\"\n",
    "python-c": "python3 -c \"print('bash {big}')\" | bash\n",
    "outer-shell": "bash -s <<'R'\npython3 - <<'PY' | bash\n" + _PRINT + "PY\nR\n",
    # Review 5890784121 nit 1: a stray quote of the other kind must not shift literal pairing.
    "apostrophe-in-double": "python3 - <<'PY' | bash\nprint(\"it's\")\n" + _PRINT + "PY\n",
    "double-in-single": "python3 - <<'PY' | bash\nprint('say \"hi')\nprint(\"bash {big}\")\nPY\n",
    "apostrophe-in-comment": "python3 - <<'PY' | bash\n# don't\n" + _PRINT + "PY\n",
    # Nit 2: producers with no heredoc (an escaped newline glued to the path, unquoted words).
    "printf-escape": "printf 'bash {big}\\n' | bash\n",
    "printf-format": "printf '%s\\n' 'bash {big}' | bash\n",
    "print-escape": "python3 - <<'PY' | bash\nprint('bash {big}\\n')\nPY\n",
    "echo-unquoted": "echo bash {big} | bash\n",
    "echo-wrapped": "timeout 5 echo bash {big} | sudo bash\n",
    "echo-eval": "eval \"$(echo bash {big})\"\n",
    "echo-function": "f() { echo bash {big}; }; f | bash\n",
    "echo-then": "if true; then echo bash {big}; fi | bash\n",
    # Echo options are not printed: `-n` must not be read as the executed command.
    "echo-n": "echo -n bash {big} | bash\n",
    "echo-ne": "echo -ne bash {big} | bash\n",
    "echo-n-e": "echo -n -e bash {big} | bash\n",
    "echo-E-eval": "eval \"$(echo -E bash {big})\"\n",
    "echo-n-wrapped": "timeout 5 echo -n bash {big} | sudo bash\n",
    "printf-dashdash": "printf -- bash\\ {big} | bash\n",
    # `function NAME {` is a definition, not the executed command: its body's echo still prints.
    "echo-function-keyword": "function f { echo bash {big}; }; f | bash\n",
    "echo-n-function-keyword": "function f { echo -n bash {big}; }; f | bash\n",
    "echo-function-keyword-parens": "function f() { echo bash {big}; }; f | bash\n",
    "echo-time-p": "time -p echo bash {big} | bash\n",
    # A printf format is not printed as written: `%s` must not be read as the executed command.
    "printf-format-two": "printf '%s %s' bash {big} | bash\n",
    "printf-format-trailing-space": "printf '%s ' bash {big} | bash\n",
    "printf-dashdash-format": "printf -- '%s %s' bash {big} | bash\n",
    "printf-format-split-word": "printf 'ba%s %s' sh {big} | bash\n",
    "printf-format-function-keyword": "function f { printf '%s %s' bash {big}; }; f | bash\n",
    "printf-variable-format": "printf \"$f\" bash {big} | bash\n",
    "printf-format-repeats": "printf '%s%s ' ba sh {big} | bash\n",
    # Review 5893864308 (PR #2341): a `*` width or precision takes an argument, a precision
    # truncates and `%b` decodes its argument, so the whole-argument walk read the wrong words.
    "printf-star-width": "printf '%*s %s' 5 bash {big} | bash\n",
    "printf-star-precision": "printf '%.*s %s' 4 bashrc {big} | bash\n",
    "printf-precision": "printf '%.4s %s' bashrc {big} | bash\n",
    "printf-b-hex": "printf '%b %s' 'ba\\x73h' {big} | bash\n",
    "printf-b-octal": "printf '%b %s' 'ba\\0163h' {big} | bash\n",
    "printf-b-stop": "printf '%b%s' 'bash {big}\\c' .bak | bash\n",  # `\c` ends all output
    "printf-format-hex": "printf 'ba\\x73h %s' {big} | bash\n",
    "echo-e-hex": "echo -e 'ba\\x73h {big}' | bash\n",
    "echo-e-octal": "echo -e 'ba\\0163h {big}' | bash\n",  # echo's `\0NNN`, not the format's
    "print-hex": "python3 - <<'PY' | bash\nprint('ba\\x73h {big}')\nPY\n",
    # Review of PR #2341 @d532aef7d4: `-v NAME` takes an operand, so the format is the next word.
    "printf-v": "printf -v x '%s %s' bash {big}; echo $x | bash\n",
    "printf-v-dashdash": "printf -v x -- '%s %s' bash {big}; echo $x | bash\n",
    "printf-v-glued": "printf -vx -- '%s %s' bash {big}; echo $x | bash\n",
    # Kept from the old opener peel (fails closed): a leading `in` is not read as the command.
    "echo-after-in": "for w\nin echo bash {big}; do :; done | bash\n",
    # Nit 3 (intended, fails closed): any later pipe word counts as the shell, a grep pattern too.
    "pipe-grep-bash": "python3 - <<'PY' | grep bash\n" + _PRINT + "PY\n",
    "pipe-grep-c-bash": "echo bash {big} | grep -c bash\n",
}


def _big(tmp_path):
    big = tmp_path / "x" / "huge.sh"
    big.parent.mkdir()
    big.write_text("# padding\n" * 130000, encoding="utf-8")  # over the 1 MiB scan cap
    return big


@pytest.mark.parametrize("shape", sorted(_SINK_SHAPES))
def test_consumer_output_fed_to_a_shell_fails_closed(tmp_path, shape):
    command = _SINK_SHAPES[shape].replace("{big}", str(_big(tmp_path)))
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe is True and refusal is not None and "scan cap" in refusal, refusal


def test_review_function_repro_is_not_masked_for_the_walk(tmp_path):
    command = _SINK_SHAPES["review-function"].replace("{big}", str(_big(tmp_path)))
    assert strip_inert_heredoc_bodies(command, nested=True) == command


@pytest.mark.parametrize("command", [
    "(\npython3 - <<'PY'\nprint(1)\nPY\n) | bash",
    "{\ncat <<'PY'\nx\nPY\n} | sudo bash",
    "o=$(\npython3 - <<'PY'\nprint(1)\nPY\n)\neval \"$o\"",
    "bash <(\npython3 - <<'PY'\nprint(1)\nPY\n)",
])
def test_multiline_consumers_feeding_a_shell_mask_nothing(command):
    assert strip_inert_heredoc_bodies(command) == command
    assert strip_inert_heredoc_bodies(command, nested=True) == command


# The consumer's output goes nowhere a shell runs it: the path stays a mention (allowed).
_NO_SINK_SHAPES = {
    "plain": "python3 - <<'PY'\n" + _PRINT + "PY\n",
    "pipe-grep": "python3 - <<'PY' | grep x\n" + _PRINT + "PY\n",
    "or-list": "python3 - <<'PY' || true\n" + _PRINT + "PY\n",
    "assign-echo": "o=$(python3 - <<'PY'\n" + _PRINT + "PY\n)\necho \"$o\"\n",
    "function-no-sink": "f() { python3 - <<'PY'\n" + _PRINT + "PY\n}; f\n",
    "subst-argument": "hermes kanban comment t_x \"$(cat /tmp/f.md)\"\npython3 - <<'PY'\n"
                      + _PRINT + "PY\n",
    "apostrophe-no-sink": "python3 - <<'PY'\nprint(\"it's\")\n" + _PRINT + "PY\n",
    "printf-no-sink": "printf 'bash {big}\\n'\n",
    "echo-no-sink": "echo bash {big}\n",
    "echo-or-list": "echo bash {big} || bash -c true\n",
    "echo-function-keyword-no-sink": "function f { echo bash {big}; }; f\n",
    "printf-format-no-sink": "printf '%s %s' bash {big}\n",
    "printf-star-width-no-sink": "printf '%*s %s' 5 bash {big}\n",
    "printf-b-hex-no-sink": "printf '%b %s' 'ba\\x73h' {big}\n",
}


@pytest.mark.parametrize("shape", sorted(_NO_SINK_SHAPES))
def test_consumer_output_not_fed_to_a_shell_stays_a_mention(tmp_path, shape):
    command = _NO_SINK_SHAPES[shape].replace("{big}", str(_big(tmp_path)))
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize("text", [
    "x | bash", "x |& sh -s", "x | sudo bash", "x | ssh h", "x | env A=1 /bin/sh",
    "x | timeout 5 bash -s", "eval \"$o\"", ". <(x)", "source /dev/stdin", "bash <(x)",
    "sh -c \"$(x)\"", "bash <<< \"$o\"", "$(x)", "a; `x`", "if true; then $(x); fi",
    "x | sudo -s", "x | zsh",
    # Deliberate (review 5890784121 nit 3): any later pipe word is taken as the shell, so a grep
    # pattern named bash is a sink. It only fails closed; see output_reaches_shell.
    "x | grep bash", "x | grep -c bash",
])
def test_output_reaches_shell(text):
    assert output_reaches_shell(text)


@pytest.mark.parametrize("text", [
    "x | grep bash.sh", "x | grep bashrc", "x || bash y.sh", "X=$(x)", "cd \"$(dirname \"$0\")\"",
    "ssh h \"python3 -c '$(printf x)'\"", ". ./venv/bin/activate", "diff <(x) y",
    "x | sed 's/^/h /'", "bash -s \"$1\" <<'R'", "echo $((1 + 2))",
])
def test_output_does_not_reach_shell(text):
    assert not output_reaches_shell(text)


def test_sink_regex_is_linear_on_masked_newline_runs():
    """Review 5890191993: a masked no-sink heredoc body is a run of newlines, and the
    command-word-substitution alternative was quadratic on it (16k lines: 1.5s -> 15.4s)."""
    text = "python3 - <<'PY'\n" + "\n" * 50_000 + "PY\n"
    started = time.monotonic()
    assert not output_reaches_shell(text)
    assert time.monotonic() - started < 2.0


def test_large_no_sink_heredoc_scan_stays_fast(tmp_path):
    body = "".join(f"x{i} = {i}\n" for i in range(8000))
    command = "python3 - <<'PY'\n" + body + "PY\n"
    started = time.monotonic()
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)
    assert time.monotonic() - started < 5.0


def test_producer_reading_an_oversized_data_file_under_a_sink_fails_closed(tmp_path):
    """Intended (review 5890191993 nit): under a sink every quoted literal is walked as executed,
    so a >1 MiB file the producer only reads still refuses on the scan cap."""
    data = tmp_path / "d" / "big.db"
    data.parent.mkdir()
    data.write_text("# padding\n" * 130000, encoding="utf-8")
    command = (f"python3 - <<'PY' | bash\nimport sqlite3\nsqlite3.connect('{data}')\n"
               "print('echo ok')\nPY\n")
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe is True and refusal is not None and "scan cap" in refusal, refusal


def test_shape_corpus_catches_a_guard_that_keeps_masking_sink_bodies(tmp_path, monkeypatch):
    """Mutant check for the corpus dimension: with sink detection off (bodies masked and printed
    literals left as mentions whatever reads the output), every sink nesting yields violations."""
    from tools import shell_heredoc
    from tests.cron import heredoc_shape_corpus as corpus

    cases = [case for case in corpus.generate_full_corpus()
             if case.s_position == "sink" and case.wrapper == "" and case.payload == "python-big"
             and case.sink in ("| bash", 'eval "$(…)"')]
    assert {case.nesting for case in cases} == set(corpus.SINK_NESTINGS)
    assert not corpus.violations(corpus.evaluate(tmp_path / "good", cases), lambda case: True)
    monkeypatch.setattr(shell_heredoc, "output_reaches_shell", lambda command: False)
    bad = corpus.violations(corpus.evaluate(tmp_path / "mutant", cases), lambda case: True)
    assert {case.nesting for case in bad} == set(corpus.SINK_NESTINGS)


@pytest.mark.parametrize(("format_text", "arguments", "printed"), [
    ("%s %s", ["bash", "/x/a.sh"], "bash /x/a.sh"),
    ("%s ", ["bash", "/x/a.sh"], "bash /x/a.sh "),  # the format repeats over the arguments
    # Width pads; precision truncates a string (it does not only add text).
    ("%-4s|%5.2s|%%", ["ab", "cdef"], "ab  |   cd|%"),
    ("%*s %s", ["5", "bash", "/x/a.sh"], " bash /x/a.sh"),  # `*` takes the width argument
    ("%.*s %s", ["4", "bashrc", "/x/a.sh"], "bash /x/a.sh"),
    ("%*.*s|", ["-6", "2", "abc"], "ab    |"),  # a negative `*` width left-justifies
    ("%*s|", ["x", "ab"], "ab|"),  # a non-number width is 0
    ("%b %s", ["ba\\x73h", "/x/a.sh"], "bash /x/a.sh"),  # `%b` decodes its argument
    ("%b|%b|%b", ["\\0101", "\\101", "a\\'b"], "A|A|a\\'b"),
    ("%b%s", ["bash /x/a.sh\\c", ".bak"], "bash /x/a.sh"),  # `\c` ends all output
    ("%.2b|", ["a\\x41bc"], "aA|"),  # precision after decoding
    ("ba\\x73h %s\\c\\0101", ["/x/a.sh"], "bash /x/a.sh\\c\b1"),  # the format's own escapes
    ("%c|%q|%d|%x|%.3d|%5.1f", ["xyz", "a b", "12abc", "255", "7", "3.14159"],
     "x|a\\ b|12|ff|007|  3.1"),
    ("%d|%d|%u|%#o|%.0d|", ["'a", "0x1f", "-1", "8", "0"], "97|31|18446744073709551615|010||"),
    ("%s|%y|%s", ["a", "b", "c"], "a|"),  # an invalid conversion ends the output (bash)
    ("x", ["bash", "/x/a.sh"], "x"),  # no conversion consumes nothing and stops at once
    ("%s", [], ""),
    ("%d|%s|", [], "0||"),  # missing arguments print as 0 or empty
])
def test_printf_output_applies_the_format(format_text, arguments, printed):
    from cron.lifecycle_guard_printf import printf_output

    assert printf_output(format_text, arguments) == printed


# Formats whose output the model must reproduce byte for byte; compared against real bash.
_BASH_PRINTF_CASES = [
    ("%*s %s", ["5", "bash", "/x/a.sh"]),
    ("%.4s %s", ["bashrc", "/x/a.sh"]),
    ("%.*s %s", ["4", "bashrc", "/x/a.sh"]),
    ("%*.*s|%-*s|%*s|", ["-6", "2", "abcdef", "4", "a", "x", "b"]),
    ("%b %s", ["ba\\x73h", "/x/a.sh"]),
    ("%b|%b|%b|%b|%b", ["\\0163", "\\163", "x\\1234", "a\\'b\\q", "\\x4g"]),
    ("%b%s", ["bash /x/a.sh\\c", ".bak"]),
    ("%5b|%.2b|", ["a\\cb", "ab"]),
    ("ba\\x73h \\101\\0101\\c\\'\\\"\\?\\q|%s", ["/x/a.sh"]),
    ("%s%s ", ["ba", "sh", "/x/a.sh"]),
    ("%c|%5c|%-3c|%.3c|", ["xyz", "a", "b", "cd"]),
    ("%q|%q|%q|%q|", ["bash", "/x/a.sh", "it's", "a b"]),
    ("%d|%i|%d|%d|%d|%d|%d|", ["12abc", "'a", "0x1f", "017", "08", "  -5", "+5"]),
    ("%u|%x|%X|%o|%#x|%#o|%#o|", ["-1", "255", "255", "8", "255", "8", "0"]),
    ("%+d|% d|%05d|%-5d|%5.3d|%.0d|%+u|", ["3", "3", "3", "3", "-7", "0", "3"]),
    ("%e|%G|%F|%5.1f|%g|%f|", ["1234.5", "0.0001", "2", "3.14159", "1e10", "abc"]),
    ("%s|%y|%s", ["a", "b", "c"]),
    ("%s %", ["a"]),
    ("%d|%s|%b|", []),
    ("a%zb%s%ls%hd|", ["z", "y", "4"]),
    ("%.s|%.0s|%5%|", ["abc", "def"]),
    ("%.1Q|%Q|%.2Q|%.2q|%(%Y-%m)T|", ["a b", "it's", "a b", "a b", "1296000"]),  # Jan 1970
    ("%.*s|%d|%i|", ["-1", "abcdef", "99999999999999999999", "-99999999999999999999"]),
    ("%f|%.1f|%g|%e|", ["'a", "0x10", "inf", "1.5x"]),
]


@pytest.mark.parametrize(("format_text", "arguments"), _BASH_PRINTF_CASES)
def test_printf_output_matches_real_bash(format_text, arguments):
    from cron.lifecycle_guard_printf import printf_output

    bash = shutil.which("bash") or ""
    if not bash:
        pytest.skip("no bash")
    version = subprocess.run([bash, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True,
                             text=True, check=True).stdout.strip()
    if int(version) < 5:
        pytest.skip(f"bash {version}: the model follows bash 5's printf")
    real = subprocess.run([bash, "-c", 'printf "$@"', "printf", format_text, *arguments],
                          capture_output=True, check=False, env={"LC_ALL": "C"}).stdout
    assert printf_output(format_text, arguments).encode("latin-1") == real


def test_printf_output_stops_after_the_round_cap():
    from cron.lifecycle_guard_printf import PRINTF_MAX_ROUNDS, printf_output

    printed = printf_output("%s,", [str(n) for n in range(PRINTF_MAX_ROUNDS + 10)])
    assert printed == "".join(f"{n}," for n in range(PRINTF_MAX_ROUNDS))


def test_printf_output_caps_huge_widths():
    from cron.lifecycle_guard_printf import printf_output

    printed = printf_output("%999999999s|%*d|%.999999999d", ["x", "999999999", "1", "2"])
    assert len(printed) < 15_000  # three fields of at most 4096 characters each


def test_printed_text_is_walked_with_each_escape_decoding():
    from cron.lifecycle_guard_printf import with_escapes_decoded

    assert list(with_escapes_decoded("ba\\x73h \\0163\\t")) == [
        "ba\\x73h \\0163\\t", "ba\\x73h \\0163\n", "bash \x0e3\t", "bash s\t"]
    assert list(with_escapes_decoded("bash x")) == ["bash x"]


@pytest.mark.parametrize(("text", "echo_style", "decoded"), [
    ("ba\\x73h \\0101\\101\\c|", False, "bash \b1A\\c|"),
    ("ba\\x73h \\0101\\101\\c|", True, "bash AA"),
    ("\\'\\u0041\\U00000042\\x\\", False, "'AB\\x\\"),
    ("\\'\\e\\t", True, "\\'\x1b\t"),
    # Past U+10FFFF Python has no character (bash prints invalid UTF-8): kept as written.
    ("\\U00110000|\\U0010FFFF", False, "\\U00110000|\U0010ffff"),
])
def test_decode_c_escapes(text, echo_style, decoded):
    from cron.lifecycle_guard_printf import decode_c_escapes

    assert decode_c_escapes(text, echo_style=echo_style) == decoded


@pytest.mark.parametrize("printer", ["printf --", "printf", "echo -n"])
def test_printer_with_no_printed_words_does_not_abort_the_walk(tmp_path, printer):
    # A crash in the payload walk falls back to the direct scan, which never opens the script.
    command = f"{printer} | bash\nbash {_big(tmp_path)}\n"
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe is True and refusal is not None and "scan cap" in refusal, refusal
