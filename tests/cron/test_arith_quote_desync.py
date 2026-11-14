"""Quote-like data must not hide a later arithmetic output sink."""

import shlex
import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.shell_arith_sink import substitution_in_arithmetic
from tools.shell_heredoc import output_reaches_shell, strip_inert_heredoc_bodies


@pytest.mark.parametrize("later", ["cat <<EOF\nplain\nEOF\n", "cat <<-EOF\n\tplain\n\tEOF\n"])
def test_reused_delimiter_cannot_hide_intervening_sink(tmp_path, later):
    big = tmp_path / "BIG"
    big.write_text("# padding\n" * 150000, encoding="utf-8")
    command = "cat <<'EOF'\nhello\nEOF\n" + SINK.replace("BIG", str(big)) + "\n" + later
    # A detected sink must cancel masking, including when a later heredoc reuses EOF.
    assert strip_inert_heredoc_bodies(command) == command
    unsafe, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe
    assert reason and "larger than the scan cap" in reason


def test_masked_body_retains_its_own_terminator():
    command = "cat <<'EOF'\ndon't\nEOF\necho ok\n"
    assert strip_inert_heredoc_bodies(command) == "cat <<'EOF'\n\nEOF\necho ok\n"

SINK = r'''x=$(( 1 + $(echo "b[\$(bash BIG)]") ))'''


@pytest.mark.parametrize("owner", ["bash", "bash -s", "env bash", "ssh host bash", "unknown"])
@pytest.mark.parametrize("prefix", ["", "cat <<'EOF'\ndata\nEOF\n"])
def test_literal_executable_heredoc_keeps_arithmetic_sink(tmp_path, owner, prefix):
    big = tmp_path / "BIG"
    big.write_text("# padding\n" * 150000, encoding="utf-8")
    command = prefix + owner + " <<'R'\n" + SINK.replace("BIG", str(big)) + "\nR\n"
    assert substitution_in_arithmetic(command, date_exempt=True)
    assert output_reaches_shell(command)
    assert strip_inert_heredoc_bodies(command) == command
    unsafe, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe
    assert reason and "larger than the scan cap" in reason


@pytest.mark.parametrize("prefix", [
    "cat <<EOF\ndon't\nEOF\n",
    "cat <<'EOF'\ndon't\nEOF\n",
    "cat <<-EOF\n\tdon't\n\tEOF\n",
    "cat <<A <<B\ndon't\nA\ncan't\nB\n",
    "printf %s $'it\\'s'\n",
])
def test_later_sink_survives_quote_like_data(prefix):
    assert substitution_in_arithmetic(prefix + SINK, date_exempt=True)
    assert output_reaches_shell(prefix + SINK)


@pytest.mark.parametrize("prefix", [
    "cat <<EOF\ndon't\nEOF\n",
    "printf %s $'it\\'s'\n",
])
def test_quote_like_data_preserves_date_exemption(prefix):
    assert not substitution_in_arithmetic(
        prefix + "x=$(( $(date +%s) + 2700 ))", date_exempt=True,
    )


@pytest.mark.parametrize("command", [
    "echo '$(echo data)",
    "echo 'broken",
    'echo "broken',
    "echo `broken $(echo data)",
    'echo "$(echo data)',
    "echo $'broken\\' $(echo data)",
])
def test_unclosed_quotes_fail_closed(command):
    assert substitution_in_arithmetic(command, date_exempt=True)


@pytest.mark.parametrize("shape", [
    "bash <<'R'\nSINK\nR\n",
    "sh -s <<'R'\nSINK\nR\n",
    "cat <<'EOF' >F\nb[$(bash BIG)]\nEOF\nbash <<'R'\nx=$(( 1 + $(cat F) ))\nR\n",
    "cat <<'R'\nSINK\nR\n",
    "cat <<'EOF'\nhello\nEOF\nSINK\ncat <<EOF\nplain\nEOF\n",
    "cat <<EOF\ndon't\nEOF\nSINK\n",
])
def test_masking_agrees_with_real_bash_execution(tmp_path, shape):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash unavailable")
    assert bash is not None
    marker = tmp_path / "executed"
    big = tmp_path / "BIG"
    big.write_text("touch " + shlex.quote(str(marker)) + "\n" + "# padding\n" * 150000)
    command = shape.replace("SINK", SINK)
    unsafe, _ = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    subprocess.run([bash, "-c", command], cwd=tmp_path, capture_output=True,
                   timeout=10, check=False)
    if marker.exists():
        assert unsafe, "real bash executed a payload the guard allowed"
    elif shape == "cat <<'R'\nSINK\nR\n":
        assert not unsafe
    else:
        # Linux /bin/sh may be dash (no recursive array arithmetic), not bash.
        assert shape.startswith("sh -s ")
        assert unsafe


@pytest.mark.parametrize("body", ["$(echo data)", "`echo data`"], ids=["dollar", "backtick"])
@pytest.mark.parametrize("date_exempt", [True, False])
def test_expanding_heredoc_substitution_fails_closed(body, date_exempt):
    # This is the helper's conservative contract, not a claim that echo is unsafe.
    # No arithmetic or unmatched quotes can make another scanner branch catch it.
    command = f"cat <<EOF\n{body}\nEOF\n"
    assert substitution_in_arithmetic(command, date_exempt=date_exempt)


@pytest.mark.parametrize("body", ["$(echo data)", "`echo data`"], ids=["dollar", "backtick"])
@pytest.mark.parametrize("date_exempt", [True, False])
def test_literal_heredoc_plain_substitution_is_not_arithmetic(body, date_exempt):
    command = f"cat <<'EOF'\n{body}\nEOF\n"
    assert not substitution_in_arithmetic(command, date_exempt=date_exempt)


def test_nested_literal_body_date_exemption_and_depth_limit():
    command = "bash <<'R'\nx=$(( $(date +%s) + 2700 ))\nR\n"
    assert not substitution_in_arithmetic(command, date_exempt=True)
    assert substitution_in_arithmetic(command, date_exempt=False)
    for index in range(35):
        command = f"bash <<'R{index}'\n{command}R{index}\n"
    assert substitution_in_arithmetic(command, date_exempt=True)
