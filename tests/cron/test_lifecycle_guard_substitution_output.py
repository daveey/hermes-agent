"""Computed shell inputs must not disappear at the substitution boundary."""
from __future__ import annotations

import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tests.cron import test_lifecycle_guard_fd_redirections as fixtures


RUN = [
    'exec 3<<<"$(cat ./r.sh)"; (exec 3<&-); bash <&3',
    'exec 3<<<"$(cat ./r.sh)"; exec 3<&- | cat; bash <&3',
    'bash -c "bash <&3" 3<<<"$(cat ./r.sh)"',
    'exec 3<<<"$(cat ./r.sh)"; bash -c "bash <&3"',
    'ba$(echo s)h ./r.sh',
    'X=$(cat ./r.sh); bash -c "$X"',
    'exec 3<<<"$(cat ./r.sh)"; bash <&3',
    'bash -c bash <<<"$(cat ./r.sh)"',
    '/bin/ba$(echo s)h ./r.sh',
    'X=$(cat ./r.sh); Y=${X}; bash -c "$Y"',
    'export X=$(cat ./r.sh); bash -c "$X"',
    'X=$(echo ./r.sh); bash "$X"',
    'X=$(echo bash); "$X" < ./r.sh',
    'X=$(cat ./r.sh); bash <<<"$X"',
    'X=$(cat ./r.sh); exec 3<<<"$X"; bash <&3',
    'exec 3<<<"$(cat ./r.sh)"; exec 4<&3; bash <&4',
    'exec 3<<<"$(cat ./r.sh)"; bash <&3-',
    'exec 3<<<"$(cat ./r.sh)"; bash -c bash <&3',
    'bash -c "bash -c bash" <<<"$(cat ./r.sh)"',
    'bash -c "cat | bash" <<<"$(cat ./r.sh)"',
    'X=$(cat ./r.sh); # an apostrophe does not quote the next line: \'\n bash -c "$X"',
    'en$(echo v) bash ./r.sh',
    'ti$(echo me) bash ./r.sh',
    'exec 3<<<"$(cat ./r.sh)"; { exec 3<&-; } | cat; bash <&3',
    'exec 3<<<"$(cat ./r.sh)"; cat | { exec 3<&-; }; bash <&3',
    'exec 3<<<"$(cat ./r.sh)"; (exec 3<&- | cat); bash <&3',
    'b$(true)ash ./r.sh', 'bash ./r$(true).sh',
    'bash $(echo ./r.sh)', 'bash `echo ./r.sh`',
    'source $(echo ./r.sh)', '. `echo ./r.sh`',
    'eval "$(cat ./r.sh)"', 'bash -c "$(cat ./r.sh)"',
    'bash <<< "$(cat ./r.sh)"',
    'env bash $(echo ./r.sh)', 'command bash $(echo ./r.sh)',
    'if true; then bash $(echo ./r.sh); fi',
    'bash -ec "$(cat ./r.sh)"', 'bash -o errexit $(echo ./r.sh)',
    'bash --rcfile $(echo ./r.sh) -ic exit',
    'bash < $(echo ./r.sh)', 'bash 3<<< "$(cat ./r.sh)" <&3',
    './r$(true).sh',
    '$(echo bash) -c "$(cat ./r.sh)"',
    '$(echo bash) < ./r.sh',
    '$(echo env) bash -c "$(cat ./r.sh)"',
    '$(echo -n ba)$(echo sh) <<< "$(cat ./r.sh)"',
    'cat ./r.sh | $(echo bash)',
    'true() { echo ./r.sh; }; bash $(true) "" -s < ./r.sh',
    'if true; then exec 3<<<"$(cat ./r.sh)"; else exec 3<&-; fi; bash <&3',
    'for x in 1 2; do if test "$x" = 2; then bash <&3; fi; exec 3<<<"$(cat ./r.sh)"; done',
    'exec 3<<<"$(cat ./r.sh)"; if false; then exec 3<&-; else bash <&3; fi',
    'exec 3<<<"$(cat ./r.sh)"; if false; then exec 3<&-; elif true; then bash <&3; fi',
]


@pytest.mark.parametrize('command', RUN)
def test_real_shell_executes_computed_input(tmp_path, command):
    fixtures._tree(tmp_path)
    (tmp_path / 'r.sh').chmod(0o755)
    assert fixtures._bash_runs(tmp_path, command)
    assert (tmp_path / 'marker').read_text() == 'called'
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0]


@pytest.mark.parametrize('command', RUN[4:8])
@pytest.mark.parametrize('dangerous', [False, True])
def test_review_shapes_have_real_execution_and_clean_twins(tmp_path, command, dangerous):
    fixtures._tree(tmp_path)
    if not dangerous:
        (tmp_path / 'r.sh').write_text(':\n')
    marker = tmp_path / 'marker'
    result = subprocess.run(
        ['/bin/bash'], input=command + '\n', text=True, encoding='utf-8',
        capture_output=True, timeout=20, check=False, cwd=tmp_path,
        env={'PATH': f"{tmp_path / 'bin'}:/usr/bin:/bin", 'MARKER': str(marker),
             'HOME': str(tmp_path)})
    assert (result.returncode, result.stdout, result.stderr) == (0, '', '')
    assert marker.read_text() == 'called' if dangerous else not marker.exists()
    # Clean computed shell inputs are intentionally uncertain too: the scanner
    # does not run cat/echo (both can be shadowed by functions) to certify bytes.
    unsafe, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe and reason and 'substitution output' in reason


@pytest.mark.parametrize('count', [15, 16, 17, 32])
@pytest.mark.parametrize('shell_sink', [False, True])
def test_variable_dependency_closure_is_sink_sensitive(tmp_path, count, shell_sink):
    fixtures._tree(tmp_path)
    command = ('X0=$(cat ./r.sh); '
               + '; '.join(f'X{i}=$X{i-1}' for i in range(1, count))
               + (f'; bash -c "$X{count-1}"' if shell_sink else '; python3 -c "print(1)"'))
    assert fixtures._bash_runs(tmp_path, command) == shell_sink
    unsafe, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe == shell_sink
    assert bool(reason) == shell_sink


@pytest.mark.parametrize('command, stdout', [
    ('exec 3<./clean.sh; case x in x) exec 3<<<"$(cat ./r.sh)";; *) exec 3<&-;; esac; exec 3<./clean.sh; bash <&3', ''),
    ('exec 3<./clean.sh; (case x in x) exec 3<<<"$(cat ./r.sh)";; esac); bash <&3', ''),
    ('exec 3<./clean.sh; case x in x) exec 3<<<"$(cat ./r.sh)";; esac; python3 -c "pass" <&3', ''),
    ('if true; then exec 3<<<"$(cat ./r.sh)"; exec 3<./clean.sh; bash <&3; fi', ''),
    ('for x in 1 2; do exec 3<<<"$(cat ./r.sh)"; exec 3<./clean.sh; bash <&3; done', ''),
    ('exec 3<<<"$(cat ./r.sh)"; if false; then exec 3<&-; fi; python3 -c "pass" <&3', ''),
    ('exec 3<<<"$(cat ./r.sh)"; f() { exec 3<&-; }; cat <&3 > /dev/null', ''),
    ('en$(echo v) python3 -c "print(1)"', '1\n'),
    ('en$(echo v) echo data', 'data\n'),
    ('exec 3<./clean.sh; (exec 3<<<"$(cat ./r.sh)"); bash <&3', ''),
    ('exec 3<./clean.sh; { exec 3<<<"$(cat ./r.sh)"; } | cat; bash <&3', ''),
    ('exec 3<./clean.sh; cat | { exec 3<<<"$(cat ./r.sh)"; }; bash <&3', ''),
])
def test_scope_and_wrapper_controls_execute_cleanly(tmp_path, command, stdout):
    fixtures._tree(tmp_path)
    (tmp_path / 'clean.sh').write_text(':\n')
    marker = tmp_path / 'marker'
    result = subprocess.run(
        ['/bin/bash'], input=command + '\n', text=True, capture_output=True,
        timeout=20, check=False, cwd=tmp_path,
        env={'PATH': f"{tmp_path / 'bin'}:/usr/bin:/bin", 'MARKER': str(marker),
             'HOME': str(tmp_path)})
    assert (result.returncode, result.stdout, result.stderr) == (0, stdout, '')
    assert not marker.exists()
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize('middle', [
    'if false; then exec 3<&-; fi',
    'if false; then exec 3<./clean.sh; fi',
    'false && exec 3<&-',
    'true || exec 3<&-',
    'false && { exec 3<&-; }',
    'f() { exec 3<&-; }',
    'function f { exec 3<&-; }',
    'function f() { exec 3<&-; }',
    'f()\n{ exec 3<&-; }',
    'if false; then exec 3<&-; else :; fi',
    'while false; do exec 3<&-; done',
    'until true; do exec 3<&-; done',
    'for x in; do exec 3<&-; done',
    'if true; then :; else exec 3<&-; fi',
    'if false; then :; elif false; then exec 3<&-; fi',
    '"if" exec 3<&-',
    'if true; then "fi" exec 3<&-; fi',
])
@pytest.mark.parametrize('dangerous', [False, True])
def test_skipped_descriptor_updates_preserve_may_input(tmp_path, middle, dangerous):
    fixtures._tree(tmp_path)
    for name in ('if', 'fi'):
        executable = tmp_path / 'bin' / name
        executable.write_text('#!/bin/sh\n:\n')
        executable.chmod(0o755)
    (tmp_path / 'clean.sh').write_text(':\n')
    if not dangerous:
        (tmp_path / 'r.sh').write_text(':\n')
    command = 'exec 3<<<"$(cat ./r.sh)"; ' + middle + '; bash <&3'
    marker = tmp_path / 'marker'
    result = subprocess.run(
        ['/bin/bash'], input=command + '\n', text=True, encoding='utf-8',
        capture_output=True, timeout=20, check=False, cwd=tmp_path,
        env={'PATH': f"{tmp_path / 'bin'}:/usr/bin:/bin", 'MARKER': str(marker),
             'HOME': str(tmp_path)})
    assert (result.returncode, result.stdout, result.stderr) == (0, '', '')
    assert marker.read_text() == 'called' if dangerous else not marker.exists()
    unsafe, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe and reason and 'substitution output' in reason


@pytest.mark.parametrize('middle', [
    'if false; then exec 3<&-; fi; exec 3<./clean.sh',
    'false && exec 3<&-; exec 3<./clean.sh',
    'f() { exec 3<&-; }; exec 3<./clean.sh',
    'while false; do exec 3<&-; done; exec 3<./clean.sh',
    '{ exec 3<./clean.sh; }',
])
def test_unconditional_descriptor_override_after_uncertain_scope(tmp_path, middle):
    fixtures._tree(tmp_path)
    (tmp_path / 'clean.sh').write_text(':\n')
    command = 'exec 3<<<"$(cat ./r.sh)"; ' + middle + '; bash <&3'
    assert not fixtures._bash_runs(tmp_path, command)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize('definition', ['f()', 'function f', 'function f()'])
@pytest.mark.parametrize('override', ['exec 3<./clean.sh', 'exec 3<&-'])
def test_function_call_reapplies_descriptor_effects(tmp_path, definition, override):
    fixtures._tree(tmp_path)
    (tmp_path / 'clean.sh').write_text(':\n')
    command = f'{definition} {{ exec 3<<<"$(cat ./r.sh)"; }}; {override}; f; bash <&3'
    assert fixtures._bash_runs(tmp_path, command)
    unsafe, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe and reason and 'substitution output' in reason


@pytest.mark.parametrize('ending', [';;', ';&', ';;&'])
def test_case_arms_preserve_descriptor_exits(tmp_path, ending):
    fixtures._tree(tmp_path)
    (tmp_path / 'clean.sh').write_text(':\n')
    # Fallthrough variants use a nonmatching second pattern, and a third arm
    # that closes the fd only on paths Bash does not take.
    second = ': ' if ending == ';&' else 'exec 3<./clean.sh '
    command = ('exec 3<./clean.sh; case x in x) exec 3<<<"$(cat ./r.sh)"'
               + ending + ' y) ' + second + ';; z) exec 3<&-;; esac; bash <&3')
    assert fixtures._bash_runs(tmp_path, command)
    unsafe, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe and reason and 'substitution output' in reason


@pytest.mark.parametrize('middle', [
    'case x in x) exec 3<<<"$(cat ./r.sh)";; *) exec 3<./clean.sh;; esac',
    'case x in (x|y) exec 3<<<"$(cat ./r.sh)";; *) exec 3<&-;; esac',
    'case x in x) case y in y) exec 3<<<"$(cat ./r.sh)";; esac;; *) exec 3<&-;; esac',
    'g() { f; }; f() { exec 3<<<"$(cat ./r.sh)"; }; g',
    'f() { exec 3<&4; }; exec 4<<<"$(cat ./r.sh)"; f',
])
@pytest.mark.parametrize('dangerous', [False, True])
def test_compound_fd_effects_have_clean_computed_twins(tmp_path, middle, dangerous):
    fixtures._tree(tmp_path)
    (tmp_path / 'clean.sh').write_text(':\n')
    if not dangerous:
        (tmp_path / 'r.sh').write_text(':\n')
    command = 'exec 3<./clean.sh; ' + middle + '; bash <&3'
    assert fixtures._bash_runs(tmp_path, command) == dangerous
    unsafe, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe and reason and 'substitution output' in reason


@pytest.mark.parametrize('suffix', [
    'bash <&3', 'f; exec 3<./clean.sh; bash <&3',
    '(f); bash <&3', 'f | cat; bash <&3',
    'f; python3 -c "pass" <&3', 'f; cat <&3 >/dev/null',
])
def test_function_descriptor_controls(tmp_path, suffix):
    fixtures._tree(tmp_path)
    (tmp_path / 'clean.sh').write_text(':\n')
    command = 'f() { exec 3<<<"$(cat ./r.sh)"; }; exec 3<./clean.sh; ' + suffix
    assert not fixtures._bash_runs(tmp_path, command)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize('middle', [
    '{ case x in x) exec 3<<<"$(cat ./r.sh)";; esac; }',
    'if true; then case x in x) exec 3<<<"$(cat ./r.sh)";; esac; fi',
    'for i in 1; do case x in x) exec 3<<<"$(cat ./r.sh)";; esac; done',
    'f() { case x in x) exec 3<<<"$(cat ./r.sh)";; esac; }; f',
    'f() case x in x) exec 3<<<"$(cat ./r.sh)";; esac; f',
    'my-f() { exec 3<<<"$(cat ./r.sh)"; }; my-f',
    'f.x() { exec 3<<<"$(cat ./r.sh)"; }; f.x',
    'function my-f { exec 3<<<"$(cat ./r.sh)"; }; my-f',
    'f() { exec 3<<<"$(cat ./r.sh)"; }; eval f',
    'f() { exec 3<<<"$(cat ./r.sh)"; }; n=f; $n',
    'f() { exec 3<<<"$(cat ./r.sh)"; }; shopt -s expand_aliases; alias g=f\ng',
])
@pytest.mark.parametrize('suffix', [
    'bash <&3', 'exec 3<./clean.sh; bash <&3',
    'python3 -c "pass" <&3', 'cat <&3 >/dev/null',
])
def test_prefixed_case_and_function_dispatch(tmp_path, middle, suffix):
    fixtures._tree(tmp_path)
    (tmp_path / 'clean.sh').write_text(':\n')
    # A definition-time open must not be mistaken for the invocation effect.
    middle = middle.replace('}; ', '}; exec 3<./clean.sh; ')
    command = 'exec 3<./clean.sh; ' + middle + '\n' + suffix
    runs = suffix == 'bash <&3'
    assert fixtures._bash_runs(tmp_path, command) == runs
    unsafe, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe == runs
    if runs:
        assert reason and 'substitution output' in reason


def test_guard_never_runs_the_producer(tmp_path):
    command = 'X=$(touch producer-ran); bash -c "$X"'
    unsafe, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe and reason and 'substitution output' in reason
    assert not (tmp_path / 'producer-ran').exists()


@pytest.mark.parametrize('command', [
    '2>/dev/$(echo null) stdbuf -o0 python3 ./clean.py',
    'command 2>/dev/$(echo null) python3 ./clean.py',
    'en$(echo v) python3 -c "print(1)"',
    'en$(echo v) echo data',
    'ti$(echo me) python3 -c "print(1)"',
    'exec 3<./clean.sh; (exec 3<<<"$(cat ./r.sh)"); bash <&3',
    'exec 3<./clean.sh; exec 3<<<"$(cat ./r.sh)" | cat; bash <&3',
    'exec 3<./clean.sh; cat | exec 3<<<"$(cat ./r.sh)"; bash <&3',
    'X0=$(echo data); ' + '; '.join(f'X{i}=$X{i-1}' for i in range(1, 16)) + '; python3 -c "print(1)"',
    'python3 $(echo ./clean.py)', 'python3 ./clean$(true).py',
    'py$(true)thon3 ./clean.py', 'python3 -c "$(cat ./clean.py)"',
    'python3 <<< "$(cat ./clean.py)"',
    'echo $(echo ./clean.py)', 'echo bash $(echo ./clean.py)',
    "echo 'bash $(echo ./clean.py)'", 'bash ./clean.sh $(echo data)',
    'bash -c : $(echo data)', 'bash -s $(echo data) < ./clean.sh',
    'bash <<< "$(echo data)" < ./clean.sh',
    'bash 2>$(echo ./log) ./clean.sh',
    'bash ./clean$((1+1)).sh',
    'X=$(cat ./clean.py); python3 -c "$X"',
    'X=$(cat ./clean.py); echo "$X"',
    'X=$(cat ./clean.py); bash -c : "$X"',
    "X=$(cat ./clean.py); bash -c 'echo $X'",
    r'X=$(cat ./clean.py); bash -c "echo \$X"',
    "echo X=$(cat ./clean.py); bash -c '$X'",
    'exec 3<<<"$(cat ./clean.py)"; python3 <&3',
    'exec 3<<<"$(cat ./clean.py)"; bash <&3 < ./clean.sh',
    'exec 3<<<"$(cat ./clean.py)"; exec 3<./clean.sh; bash <&3',
    'exec 3<<<"$(cat ./clean.py)"; exec 3<&-; bash <&3',
    'exec 3<<<"$(cat ./clean.py)"; exec 4<&3-; bash <&3',
    'bash 3<<<"$(cat ./clean.py)" -c :; bash <&3',
    'bash -c : <<<"$(cat ./clean.py)"',
    'bash -c python3 <<<"$(cat ./clean.py)"',
    'bash -c "bash < ./clean.sh" <<<"$(cat ./clean.py)"',
])
def test_non_shell_inputs_remain_allowed(tmp_path, command):
    fixtures._tree(tmp_path)
    (tmp_path / 'clean.sh').write_text(':\n')
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


def test_uncertainty_is_named(tmp_path):
    unsafe, reason = scan_gateway_lifecycle('bash $(echo missing.sh)', cwd=str(tmp_path))
    assert unsafe and reason and 'substitution output' in reason


@pytest.mark.parametrize('command', [
    'bash -c "echo $(date)"', 'eval "$(ssh-agent -s)"',
    'bash <<< "echo $(date)"',
])
def test_unknown_output_is_not_evaluated(command):
    # Even common benign producers can be functions. This is an explicit
    # uncertainty refusal, not a claim that their output restarts the gateway.
    unsafe, reason = scan_gateway_lifecycle(command)
    assert unsafe and reason and 'substitution output' in reason


@pytest.mark.parametrize(('command', 'uncertain', 'variant'), [
    ('echo $(true)', False, 'echo '),
    ('echo $((1+1))', False, None),
    ('f(bash $(echo script))', False, 'f(bash )'),
    ('A=$(true)', False, 'A='),
    ('bash $(echo script)', True, None),
    ('source $(echo script)', True, None),
    ('eval "$(echo code)"', True, None),
    ('b$(true)ash clean.sh', True, None),
    ('$(echo bash) -c :', True, None),
    ('bash -s $(echo data)', False, 'bash -s '),
    ('bash <<< "$(echo code)" < clean.sh', False, 'bash <<< "" < clean.sh'),
    ('bash 3<<< "$(echo code)" <&3', True, None),
    ('python3 $(echo clean.py)', False, 'python3 '),
])
def test_output_classification(command, uncertain, variant):
    from cron.lifecycle_guard_substitution_output import computed_shell_input

    assert computed_shell_input(command, frozenset()) == (uncertain, variant)


@pytest.mark.parametrize('prefix', ['', 'prefix'])
def test_empty_output_does_not_invent_literal_bindings(prefix):
    from cron import lifecycle_guard as guard

    budget = guard._LifecycleScanBudget(frozenset())
    command = f'HB={prefix}$(true); "$HB" data "$(cat missing)"'
    assert not guard._contains_unsafe_gateway_action(
        command, cwd=None, depth=0, visited=set(), budget=budget)
    assert 'HB' not in budget.variable_candidates.values


def test_empty_output_script_keeps_real_variable_discovery(tmp_path):
    fixtures._tree(tmp_path)
    (tmp_path / 'wrapper.sh').write_text('R=./r.sh; "$R"\n')
    command = './wrapper$(true).sh'
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0]


def test_mentioned_file_has_no_output_uncertainty():
    from cron import lifecycle_guard as guard

    budget = guard._LifecycleScanBudget(frozenset())
    assert not guard._contains_unsafe_gateway_action(
        'bash $(echo missing.sh)', cwd=None, depth=0, visited=set(),
        budget=budget, executed=False)
