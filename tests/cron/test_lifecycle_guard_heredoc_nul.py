"""ANSI-C NUL truncation is per quoted fragment, not per heredoc word (PR #3020)."""

from __future__ import annotations

from functools import lru_cache
import shlex
import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.shell_heredoc import _parse_heredoc_operator

LIFECYCLE = " ".join(["hermes", "gateway", "re" + "start"])
WORDS = [
    (r"$'E\0junk'", "E"),
    (r"$'E\00junk'", "E"),
    (r"$'E\000junk'", "E"),
    (r"$'E\x0junk'", "E"),
    (r"$'E\x00junk'", "E"),
    (r"$'E\u0000junk'", "E"),
    (r"$'E\U00000000junk'", "E"),
    (r"$'E\c@junk'", "E"),
    (r"$'E\c`junk'", "E"),
    (r"$'E\0junk'tail", "Etail"),
    (r"$'E\0'$'F'", "EF"),
    (r'''pre"Q"$'E\x00junk'"R"'S'tail''', "preQERStail"),
    (r"$'\0junk'E", "E"),
    (r"$'E\0ignored\'tail'F", "EF"),
    (r"$'E\x45'", "EE"),
]


@lru_cache(maxsize=1)
def _bash32() -> bool:
    result = subprocess.run(["bash", "--version"], capture_output=True, text=True,
                            timeout=20, check=True)
    return "version 3.2." in result.stdout


def _assert_bash_marker(result, marker: str, *, bash32_rejects: bool):
    # These exact modern-terminator fixtures are syntax errors on Bash 3.2,
    # not successful execution proofs. Assert that boundary rather than skip it.
    if _bash32() and bash32_rejects:
        assert result.returncode == 2, result.stderr
        assert result.stdout == ""
        assert "syntax error" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert marker in result.stdout.splitlines(), result.stderr
        assert result.stderr == ""


def _group(word, terminator, script, sink):
    return (f"{{\ncat >/dev/null <<{word}\n}}\n{terminator}\n"
            f"cat {shlex.quote(str(script))}\n}} | {sink}")


@pytest.mark.parametrize(("word", "terminator"), WORDS)
def test_nul_fragment_parser_preserves_later_fragments(word, terminator):
    opener = f"<<{word}"
    assert _parse_heredoc_operator(opener, 0) == (len(opener), terminator, False, True)


@pytest.mark.parametrize(("word", "terminator"), WORDS)
def test_nul_fragment_group_feed_is_scanned(tmp_path, word, terminator):
    script = tmp_path / "f.py"
    script.write_text(f"import os\nos.system({LIFECYCLE!r})\n", encoding="utf-8")
    assert scan_gateway_lifecycle(_group(word, terminator, script, "python3")) == (True, None)
    # The same suspicious file is only printed, not executed, without an interpreter sink.
    assert scan_gateway_lifecycle(_group(word, terminator, script, "cat")) == (False, None)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(("word", "terminator"), WORDS)
def test_nul_fragment_real_bash_and_harmless_control(tmp_path, word, terminator):
    # This is the only executed fixture: no lifecycle action ever reaches a subprocess.
    script = tmp_path / "marker.py"
    script.write_text("print('MARKER_EXECUTED')\n", encoding="utf-8")
    command = _group(word, terminator, script, "python3")
    result = subprocess.run(["bash", "--noprofile", "--norc", "-c", command],
                            capture_output=True, text=True, encoding="utf-8", timeout=20,
                            check=False)
    _assert_bash_marker(result, "MARKER_EXECUTED",
                        bash32_rejects=word in (r"$'E\u0000junk'", r"$'E\U00000000junk'"))
    assert scan_gateway_lifecycle(command) == (False, None)
