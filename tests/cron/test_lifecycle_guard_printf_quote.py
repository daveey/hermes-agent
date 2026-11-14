"""Bash quoting is representation-sensitive when printf applies precision."""

import shlex
import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from cron.lifecycle_guard_printf import printf_output, printed_argument_lines
from tests.cron.test_lifecycle_guard_output_sink import test_printf_output_matches_real_bash as _compare


@pytest.mark.parametrize("argument", [
    *[chr(n) for n in range(1, 32)], "\x7f", "a\tb", "\n'\\\"?", "plain", "", "a b",
])
@pytest.mark.parametrize("format_text", ["%q", "%.0q", "%.3q", "%12.5q", "%-12.5q",
                                         "%Q", "%.0Q", "%.1Q", "%.3Q", "%12.2Q"])
def test_control_quote_matches_bash(argument, format_text):
    _compare(format_text, [argument])


def test_quoted_control_model_keeps_fallbacks():
    lines = set(printed_argument_lines("printf", ["%.3q%s", "a\tb", "suffix"]))
    assert printf_output("%.3q%s", ["a\tb", "suffix"]) in lines
    assert "a\tbsuffix" in lines
    assert "a\tb suffix" in lines


@pytest.mark.parametrize("sink", ["{producer} | bash", 'eval "$({producer})"',
                                  "bash <({producer})"])
def test_control_quote_precision_under_shell_sink(tmp_path, sink):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash")
    script = tmp_path / "payload.sh"
    script.write_text("printf marker", encoding="utf-8")
    # %.3q keeps `$'b`, not `b` plus a backslash-quoted literal tab.
    producer = "printf " + shlex.join([r"%.3qash\047 %s", "b\t", str(script)])
    command = sink.format(producer=producer)
    real = subprocess.run([bash], input=command.encode(), capture_output=True,
                          check=True, timeout=10, env={"LC_ALL": "C"})
    assert real.stdout == b"marker"
    # The exact same executed path must be walked, not merely mentioned.
    script.write_text("# padding\n" * 130000, encoding="utf-8")
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe and refusal and "scan cap" in refusal


@pytest.mark.parametrize(("quoted", "decoded"), [
    (r"$'a\tb'", "a\tb"), (r"$'a\001b'", "a\x01b"),
    (r"$'a\'b\\c'", "a'b\\c"), ("$'abc", "$abc"),
])
def test_quote_variant_preserves_word_contents(quoted, decoded):
    from cron.lifecycle_guard_printf import _shell_quote_variant

    if quoted.endswith("'"):
        assert shlex.split(_shell_quote_variant(quoted)) == [decoded]
    else:
        assert _shell_quote_variant(quoted) == quoted

