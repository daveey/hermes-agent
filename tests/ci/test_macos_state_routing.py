"""Replay the macOS state lane's real workflow wiring, without running a mini job."""
import json
import os
import subprocess
import sys
from pathlib import Path

import hermes_yaml
import pytest

from scripts.ci.classify_changes import classify
from scripts.ci.required_results import evaluate_macos_state
from tests.ci import _gha_expr as gha, workflow_steps

ROOT = Path(__file__).resolve().parents[2]


def workflow(name):
    doc = hermes_yaml.safe_load((ROOT / '.github/workflows' / name).read_text())
    doc['on'] = doc.get('on', doc.get(True, {}))
    return doc


def context(event='pull_request', ref='refs/pull/1/merge', release=False, fork=False):
    return {'github': {'event_name': event, 'ref': ref, 'sha': 'trigger-sha',
                       'run_id': '123', 'run_attempt': '1', 'repository': 'owner/repo',
                       'event': {'pull_request': {'head': {'repo': {
                           'full_name': 'fork/repo' if fork else 'owner/repo'}}}}},
            'inputs': {'release': release, 'commit': ''},
            '__status__': {'always': lambda: True}}


def detect(paths, ctx):
    raw = {key: gha.to_string(value) for key, value in classify(paths).items()}
    action = hermes_yaml.safe_load((ROOT / '.github/actions/detect-changes/action.yml').read_text())
    outputs = {key: gha.render(value['value'], {'steps': {'classify': {'outputs': raw}}})
               for key, value in action['outputs'].items()}
    job = workflow('ci.yaml')['jobs']['detect']
    ctx['steps'] = {'classify': {'outputs': outputs}}
    step = next(step for step in job['steps'] if step.get('id') == 'gate-lanes')
    ctx['steps']['gate-lanes'] = {'outputs': workflow_steps.outputs(step, ctx)}
    return {key: gha.render(value, ctx) for key, value in job['outputs'].items()}


@pytest.mark.parametrize('event,ref,release,selected', [
    ('push', 'refs/heads/main', False, False),
    ('push', 'refs/heads/other', False, True),
    ('push', 'refs/heads/main', True, True),
    ('workflow_dispatch', 'refs/heads/main', False, True),
    ('workflow_call', 'refs/heads/main', True, True),
    ('pull_request', 'refs/pull/1/merge', False, True),
])
def test_only_nonrelease_main_push_is_masked(event, ref, release, selected):
    assert detect([], context(event, ref, release))['hermes_state'] == gha.to_string(selected)


def test_release_forces_unselected_lane_and_explicit_commit_is_not_masked():
    assert detect(['README.md'], context(release=True))['hermes_state'] == 'true'
    ctx = context('push', 'refs/heads/main')
    ctx['inputs']['commit'] = 'candidate-sha'
    assert detect([], ctx)['hermes_state'] == 'true'


@pytest.mark.parametrize('paths,selected', [
    (['hermes_state.py'], True), (['hermes_state_sessions.py'], True),
    (['tests/hermes_state/test_state.py'], True), (['tests/conftest.py'], True),
    (['scripts/run_tests.sh'], True), (['scripts/run_tests_parallel.py'], True),
    (['scripts/run_tests_host_slots.py'], True), (['tests/_fixtures/state.py'], True),
    (['scripts/run_tests_parallel_rss.py'], True),
    (['pyproject.toml'], True), (['uv.lock'], True), (['setup.py'], True),
    (['.github/workflows/ci.yaml'], True), ([], True),
    ([f'docs/{i}.md' for i in range(300)], True),
    ([f'docs/{i}.md' for i in range(299)], False),
    (['README.md'], False), (['run_agent.py'], False),
])
@pytest.mark.parametrize('fork', [False, True])
def test_classifier_action_detect_and_caller_chain(paths, selected, fork):
    ctx = context(fork=fork)
    outputs = detect(paths, ctx)
    assert outputs['hermes_state'] == gha.to_string(selected)
    ctx['needs'] = {'detect': {'result': 'success', 'outputs': outputs}}
    job = workflow('ci.yaml')['jobs']['hermes-state-macos']
    assert gha.condition(job['if'], ctx) is (selected and not fork)
    result = 'success' if selected and not fork else 'skipped'
    ctx['needs']['hermes-state-macos'] = {'result': result}
    assert evaluate_macos_state(ctx['needs'])['ok'] is (not selected or not fork)


def test_reusable_contract_commit_and_independent_concurrency():
    ci = workflow('ci.yaml')
    caller = ci['jobs']['hermes-state-macos']
    child = workflow(Path(caller['uses']).name)
    assert 'workflow_call' in child['on']
    assert child['on']['workflow_call']['inputs']['commit']['default'] == ''
    contexts = [context('push', 'refs/heads/main'), context('workflow_dispatch', 'refs/heads/main')]
    contexts[1]['github']['run_id'] = '124'
    contexts[1]['inputs']['commit'] = 'admitted-sha'
    groups = []
    for ctx in contexts:
        ctx['inputs'] = {key: gha.render(value, ctx) for key, value in caller['with'].items()}
        job = child['jobs']['hermes-state-macos']
        checkout = next(step for step in job['steps'] if workflow_steps.is_checkout(step))
        assert gha.render(checkout['with']['ref'], ctx) == (ctx['inputs']['commit'] or 'trigger-sha')
        groups.append(gha.render(child['concurrency']['group'], ctx))
    assert groups[0] != groups[1]
    assert child['concurrency']['cancel-in-progress'] is False
    assert ci['jobs']['all-checks-pass']['name'] == 'All required checks pass (v2)'
    consumer = ci['jobs']['macos-state-required']
    assert consumer['name'] == 'macOS state required (v1)'
    assert set(consumer['needs']) == {'detect', 'hermes-state-macos'}
    assert gha.condition(consumer['if'], context())


def test_standalone_pushes_collapse_without_superseding_required_calls():
    group = workflow('hermes-state-macos.yml')['concurrency']['group']
    def render(workflow_name, run_id, event='push'):
        ctx = context(event, 'refs/heads/main')
        ctx['github'].update(workflow=workflow_name, run_id=run_id, sha=f'sha-{run_id}')
        return gha.render(group, ctx)
    assert render('hermes_state (macOS)', 1) == render('hermes_state (macOS)', 2)
    assert render('CI', 1) != render('CI', 2)
    assert render('CI', 1) != render('hermes_state (macOS)', 1)
    assert render('hermes_state (macOS)', 1, 'workflow_dispatch') != render('hermes_state (macOS)', 2, 'workflow_dispatch')


@pytest.mark.parametrize('selected', ['true', 'false', None, '', 'TRUE', 0, 1, True, False])
@pytest.mark.parametrize('result', ['success', 'skipped', 'failure', 'cancelled', None])
def test_required_consumer_executes_real_cli(selected, result):
    needs = {'detect': {'result': 'success', 'outputs': {'hermes_state': selected}}}
    if result is not None:
        needs['hermes-state-macos'] = {'result': result}
    ctx = context()
    ctx['needs'] = needs
    consumer = workflow('ci.yaml')['jobs']['macos-state-required']
    step = next(step for step in consumer['steps'] if 'run' in step)
    proc = subprocess.run(['bash', '-eo', 'pipefail', '-c', gha.render(step['run'], ctx)],
                          cwd=ROOT, text=True, capture_output=True, timeout=30, check=False,
                          env={**os.environ, 'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'],
                               **{k: gha.to_string(gha.render(v, ctx)) for k, v in step['env'].items()}})
    ok = (selected == 'true' and result == 'success') or (selected == 'false' and result in ('success', 'skipped'))
    assert proc.returncode == (0 if ok else 1), proc.stdout + proc.stderr
    assert ('::error::' in proc.stdout) is not ok


@pytest.mark.parametrize('result', ['failure', 'cancelled', 'skipped', None])
def test_unknown_detection_fails_closed(result):
    needs: dict = {'hermes-state-macos': {'result': 'success'}}
    if result is not None:
        needs['detect'] = {'result': result, 'outputs': {'hermes_state': 'false'}}
    assert not evaluate_macos_state(needs)['ok']
    assert not evaluate_macos_state({})['ok']
    assert not evaluate_macos_state(None)['ok']


@pytest.mark.parametrize('fork', [False, True])
def test_reusable_job_also_rejects_fork_prs(fork):
    child = workflow('hermes-state-macos.yml')['jobs']['hermes-state-macos']
    assert gha.condition(child['if'], context(fork=fork)) is not fork


@pytest.mark.parametrize('code', [0, 1])
def test_suite_launch_receipt_and_failure_propagation(tmp_path, code):
    """Execute shell wiring; receipt taskpolicy's launch boundary, not native macOS execution."""
    job = workflow('hermes-state-macos.yml')['jobs']['hermes-state-macos']
    step = next(step for step in job['steps'] if 'run' in step)
    workflow_steps.required(step, context())
    receipt = tmp_path / 'receipt.json'
    shim = '''taskpolicy() {
      "$REPLAY_PYTHON" -c 'import json,os,sys; from pathlib import Path; Path(os.environ["RECEIPT"]).write_text(json.dumps({"args": sys.argv[1:], "home": os.environ["HERMES_HOME"], "exists": Path(os.environ["HERMES_HOME"]).is_dir()}))' "$@"
      return "$SUITE_EXIT"
    }
    '''
    proc = subprocess.run(['bash', '-eo', 'pipefail', '-c', shim + step['run']],
                          cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
                          env={**os.environ, **step.get('env', {}), 'RUNNER_TEMP': str(tmp_path),
                               'REPLAY_PYTHON': sys.executable, 'RECEIPT': str(receipt),
                               'SUITE_EXIT': str(code)})
    assert proc.returncode == code, proc.stderr
    call = json.loads(receipt.read_text())
    assert call['args'] == ['-c', 'utility', 'scripts/run_tests.sh', 'tests/hermes_state/', '-j', '2']
    assert call['exists']
    assert Path(call['home']).parent == tmp_path
    assert not Path(call['home']).exists(), 'throwaway home must be removed on failure too'
