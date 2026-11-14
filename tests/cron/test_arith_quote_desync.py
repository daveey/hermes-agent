"""Quote-like data must not hide a later arithmetic output sink."""

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
