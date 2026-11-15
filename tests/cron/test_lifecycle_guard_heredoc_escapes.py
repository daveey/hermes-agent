"""Bash control-backslash and unencodable ANSI-C delimiter regressions."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tests.cron.test_lifecycle_guard_heredoc_nul import LIFECYCLE, _group
from tools.shell_heredoc import _parse_heredoc_operator
from tools.shell_heredoc_words import _decode_ansi_c

CASES = [
    (r"E\c\\", "E\x1c"),
    (r"E\c\\x", "E\x1cx"),
    (r"E\U80000000", "E"),
    (r"E\UFFFFFFFE", "E"),
    (r"E\UFFFFFFFF", "E"),
    (r"E\UFFFFFFFFZ", "EZ"),
    (r"E\UFFFFFFFFF", "EF"),
    (r"E\U0010FFFF", "E\U0010FFFF"),
    (r"E\uD7FF", "E\uD7FF"),
    (r"E\uE000", "E\uE000"),
    (r"E\0ignored", "E"),
    (r"E\c\\\0ignored", "E\x1c"),
    (r"E\UFFFFFFFF\0ignored", "E"),
]


@pytest.mark.parametrize(("fragment", "decoded"), CASES)
def test_decoder(fragment, decoded):
    assert _decode_ansi_c(fragment) == decoded
    opener = f"<<$'{fragment}'"
    assert _parse_heredoc_operator(opener, 0) == (len(opener), decoded, False, True)


@pytest.mark.parametrize(("fragment", "decoded"), CASES)
@pytest.mark.parametrize("suffix", ["", "tail", "$'F'"])
def test_group_feed(tmp_path, fragment, decoded, suffix):
    word = f"$'{fragment}'{suffix}"
    terminator = decoded + ("F" if suffix == "$'F'" else suffix)
    script = tmp_path / "f.py"
    script.write_text(f"import os\nos.system({LIFECYCLE!r})\n", encoding="utf-8")
    assert scan_gateway_lifecycle(_group(word, terminator, script, "python3")) == (True, None)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(("fragment", "decoded"), CASES)
@pytest.mark.parametrize("suffix", ["", "tail", "$'F'"])
def test_bash_marker_and_harmless_controls(tmp_path, fragment, decoded, suffix):
    word = f"$'{fragment}'{suffix}"
    terminator = decoded + ("F" if suffix == "$'F'" else suffix)
    marker = tmp_path / "marker.py"
    marker.write_text("print('MARKER_EXECUTED')\n", encoding="utf-8")
    command = _group(word, terminator, marker, "python3")
    result = subprocess.run(["bash", "--noprofile", "--norc"], input=command,
                            capture_output=True, text=True, encoding="utf-8", timeout=20,
                            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}, check=False)
    assert (result.returncode, result.stdout, result.stderr) == (0, "MARKER_EXECUTED\n", "")
    assert scan_gateway_lifecycle(command) == (False, None)
    marker.write_text(f"import os\nos.system({LIFECYCLE!r})\n", encoding="utf-8")
    # Never execute this file: a non-interpreter sink only prints its source.
    assert scan_gateway_lifecycle(_group(word, terminator, marker, "cat")) == (False, None)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(("escape", "hex_bytes"), [
    (r"\U0010FFFF", "f48fbfbf"),
    (r"\U00110000", "f4908080"),
    (r"\U7FFFFFFF", "fdbfbfbfbfbf"),
    (r"\U80000000", ""),
    (r"\UFFFFFFFF", ""),
    (r"\uD800", "eda080"),
    (r"\uDFFF", "edbfbf"),
])
def test_bash_byte_boundaries(escape, hex_bytes):
    # Bash supports legacy UTF-8 above Unicode's limit until signed 32-bit overflow.
    # Those non-Unicode bytes cannot be a terminator in a UTF-8 str command.
    result = subprocess.run(["bash", "--noprofile", "--norc"],
                            input=f"printf %s $'{escape}'\n".encode(), capture_output=True,
                            timeout=20, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"},
                            check=False)
    assert (result.returncode, result.stdout, result.stderr) == (0, bytes.fromhex(hex_bytes), b"")
    if escape in (r"\U00110000", r"\U7FFFFFFF"):
        assert _decode_ansi_c(escape) == escape

