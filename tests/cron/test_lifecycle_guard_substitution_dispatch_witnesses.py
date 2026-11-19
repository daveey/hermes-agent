"""Dynamic-dispatch policy witnesses, including conservative may-effects.

Malformed expanded/equals headers are parser-policy controls, not claims about
executable Bash syntax. Repeated definitions deliberately retain may-effects.
"""
import pytest

from cron.lifecycle_guard_substitution_output import computed_shell_input


@pytest.mark.parametrize(('command', 'expected'), [
    ('f() { exec 3<<<"$(cat ./r.sh)"; }; f() { exec 3<&-; }; exec 3<&-; f; bash <&3', True),
    ('f() { exec 3<<<"$(cat ./r.sh)"; }; exec 3<&-; $(printf f); bash <&3', True),
    ('f() { exec 3<<<"$(cat ./r.sh)"; }; exec 3<&-; eval; bash <&3', False),
    ('function case { exec 3<<<"$(cat ./r.sh)"; }; exec 3<&-; "case"; bash <&3', True),
    ('=() { exec 3<<<"$(cat ./r.sh)"; }; exec 3<&-; =; bash <&3', False),
    ('"$f"() { exec 3<<<"$(cat ./r.sh)"; }; exec 3<&-; "$f"; bash <&3', False),
    ('function f() { exec 3<<<"$(cat ./r.sh)"; }; exec 3<&-; f; bash <&3', True),
    # Malformed syntax controls only, not executable-shell safety witnesses.
    ('"a<b"() { exec 3<<<"$(cat ./r.sh)"; }; exec 3<&-; "a<b"; bash <&3', False),
    ('"a>b"() { exec 3<<<"$(cat ./r.sh)"; }; exec 3<&-; "a>b"; bash <&3', False),
    ('f( ""() { exec 3<<<"$(cat ./r.sh)"; }; exec 3<&-; f; bash <&3', True),
])
def test_dispatch_policy_witness(command, expected):
    assert computed_shell_input(command, frozenset())[0] is expected
