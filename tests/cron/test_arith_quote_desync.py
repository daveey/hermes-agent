"""Quote-like data must not hide a later arithmetic output sink."""

import pytest

from tools.shell_arith_sink import substitution_in_arithmetic
from tools.shell_heredoc import output_reaches_shell

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
