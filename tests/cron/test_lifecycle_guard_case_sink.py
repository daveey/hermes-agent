"""Case pattern closers must not turn substitution data into executed scripts."""
import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.shell_arith_sink import substitution_in_arithmetic
from tools.shell_heredoc import output_reaches_shell


@pytest.mark.parametrize('body', [
    'case x in x) echo " # ";; esac; echo ok',
    'case x in (x) echo " # ";; esac; echo ok',
    'case x in x) case y in y) echo " # ";; esac;; esac',
])
@pytest.mark.parametrize('levels', [1, 2, 3])
def test_case_substitution_output_is_data(body, levels):
    for _ in range(levels):
        body = 'echo "$(' + body + ')"'
    assert not substitution_in_arithmetic(body, date_exempt=True)
    assert not output_reaches_shell(body)


@pytest.mark.parametrize('payload', [
    'echo "$(( $(echo 1) + 1 ))"',
    'let "a[$(echo 1)]=1"',
    'declare -i value; value=$(echo 1)',
])
def test_case_body_retains_real_arithmetic_sink(payload):
    command = 'echo "$(echo "$(case x in x) ' + payload + ';; esac)")"'
    assert substitution_in_arithmetic(command, date_exempt=True)
    assert output_reaches_shell(command)


@pytest.mark.parametrize('command', ['echo "$(echo x', 'echo $(case x in x) echo x;; esac'])
def test_incomplete_substitution_stays_conservative(command):
    assert substitution_in_arithmetic(command, date_exempt=True)


def test_substitution_boundary_budget_stays_conservative(monkeypatch):
    from tools import shell_prefix_expansions as substitutions

    def exhausted(*args):
        raise RecursionError('test boundary budget')

    monkeypatch.setattr(substitutions, '_expansion_end', exhausted)
    assert substitution_in_arithmetic('echo $(echo ok)', date_exempt=True)


@pytest.mark.parametrize('date_exempt', [True, False])
def test_nested_body_preserves_date_policy(date_exempt):
    command = 'echo "$(echo "$(( $(date +%s) + 1 ))")"'
    assert substitution_in_arithmetic(command, date_exempt=date_exempt) is not date_exempt


def test_nested_body_charges_recursion_depth():
    assert substitution_in_arithmetic('echo $(echo ok)', date_exempt=True, _depth=31)


@pytest.mark.parametrize('levels, expected', [(10, False), (20, True)])
def test_executable_heredoc_keeps_enclosing_substitution_depth(levels, expected):
    inner = 'echo $(' * levels + 'echo ok' + ')' * levels
    command = 'echo $(' * levels + "bash <<'EOF'\n" + inner + '\nEOF\n' + ')' * levels
    assert substitution_in_arithmetic(command, date_exempt=True) is expected


def test_case_body_executed_device_still_fails_closed():
    command = 'echo "$(echo "$(case x in x) echo " # ";; esac; bash /dev/null)")"'
    unsafe, reason = scan_gateway_lifecycle(command)
    assert unsafe
    assert 'not a regular file' in reason
