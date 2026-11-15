"""The required aggregate gates a PR on the macOS native-memory lane exactly when it touches the reader.

The Darwin memory reader's no-subprocess tests are ``platforms("macos")``: every
Linux lane skips them, so a PR that put ``subprocess.run`` back into
``_mem_used_pct`` passed all Linux CI. Only native-memory-macos.yml executes them.
This replays the real wiring (classifier -> detect action -> ci.yaml detect ->
caller job -> "All required checks pass (v2)" evaluate step, run as the real CLI)
without a mini job, for a PR touching the reader and for one touching none of its
paths.
"""
import os
import subprocess
import sys
from pathlib import Path

import hermes_yaml
import pytest

from scripts.ci import classify_changes
from scripts.ci.classify_changes import classify
from tests.ci import _gha_expr as gha, workflow_steps

ROOT = Path(__file__).resolve().parents[2]
CALLER = 'native-memory-macos'
GATE = 'All required checks pass (v2)'
# The lane's six paths (native-memory-macos.yml push.paths; classify_changes.py NATIVE_MEMORY_PATHS).
MEMORY_PATHS = (
    'hermes_bootstrap.py',
    'hermes_cli/launch_malloc.py',
    'hermes_cli/venv_sync.py',
    'tests/test_bootstrap_malloc.py',
    'hermes_cli/machine_memory_darwin.py',
    'hermes_cli/machine_metrics.py',
    'tests/hermes_cli/test_machine_memory_darwin.py',
    'tests/hermes_cli/test_machine_memory_nofork.py',
    '.github/workflows/native-memory-macos.yml',
    'tests/ci/test_native_memory_workflow.py',
)


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
            '__status__': {'always': lambda: True, 'cancelled': lambda: False}}


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


def caller_job():
    jobs = workflow('ci.yaml')['jobs']
    callers = [name for name, job in jobs.items()
               if job.get('uses') == './.github/workflows/native-memory-macos.yml']
    assert callers == [CALLER], 'ci.yaml must call native-memory-macos.yml exactly once, as the required lane'
    return jobs[CALLER]


def gate_job():
    gate = workflow('ci.yaml')['jobs']['all-checks-pass']
    assert gate['name'] == GATE
    return gate


def run_gate(needs):
    """Run the aggregate's real "Evaluate job results" step over ``needs``; True when it passes."""
    ctx = context()
    ctx['needs'] = needs
    step = next(step for step in gate_job()['steps'] if step.get('id') == 'evaluate')
    env = {**os.environ, 'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'],
           **{k: gha.to_string(gha.render(v, ctx)) for k, v in step['env'].items()}}
    env.pop('GITHUB_OUTPUT', None)
    proc = subprocess.run(['bash', '-eo', 'pipefail', '-c', gha.render(step['run'], ctx)],
                          cwd=ROOT, text=True, capture_output=True, timeout=30, check=False, env=env)
    assert proc.returncode in (0, 1), proc.stdout + proc.stderr
    return proc.returncode == 0


def pr_needs(paths, fork=False, lane_result=None):
    """The aggregate's ``needs`` for a PR with this diff: every other job green or skipped."""
    ctx = context(fork=fork)
    outputs = detect(paths, ctx)
    ctx['needs'] = {'detect': {'result': 'success', 'outputs': outputs}}
    runs = gha.condition(caller_job()['if'], ctx)
    needs = {name: {'result': 'success'} for name in gate_job()['needs']}
    needs['detect'] = ctx['needs']['detect']
    needs[CALLER] = {'result': lane_result or ('success' if runs else 'skipped')}
    return outputs, runs, needs


def test_aggregate_waits_for_the_native_memory_job():
    assert CALLER in gate_job()['needs']
    assert caller_job()['needs'] == 'detect'


@pytest.mark.parametrize('lane_result', ['skipped', 'failure', 'cancelled', None])
def test_pr_touching_machine_metrics_requires_the_native_memory_job(lane_result, monkeypatch):
    # No duplicate run exists in this replay; never query the host's real CI run.
    monkeypatch.delenv('GITHUB_REPOSITORY', raising=False)
    outputs, runs, needs = pr_needs(['hermes_cli/machine_metrics.py'])
    assert outputs['native_memory'] == 'true'
    assert runs is True
    assert run_gate(needs) is True
    if lane_result is None:
        del needs[CALLER]  # never reported, e.g. still pending when read
    else:
        needs[CALLER] = {'result': lane_result}
    assert run_gate(needs) is False


@pytest.mark.parametrize('path', MEMORY_PATHS)
def test_every_memory_path_selects_the_lane(path, monkeypatch):
    monkeypatch.delenv('GITHUB_REPOSITORY', raising=False)
    outputs, runs, needs = pr_needs([path, 'README.md'])
    assert outputs['native_memory'] == 'true' and runs is True
    needs[CALLER] = {'result': 'skipped'}
    assert run_gate(needs) is False


@pytest.mark.parametrize('paths', [
    ['README.md'], ['run_agent.py'], ['hermes_cli/main.py'], ['hermes_cli/machine_memory.py'],
    ['tests/hermes_cli/test_machine_metrics.py'], ['.github/workflows/ci.yaml'],
    ['scripts/ci/classify_changes.py', 'tests/ci/test_native_memory_gate.py'],
])
def test_pr_touching_no_memory_path_passes_without_the_macos_job(paths, monkeypatch):
    monkeypatch.delenv('GITHUB_REPOSITORY', raising=False)
    outputs, runs, needs = pr_needs(paths)
    assert outputs['native_memory'] == 'false'
    assert runs is False
    assert needs[CALLER] == {'result': 'skipped'}
    assert run_gate(needs) is True


def test_fork_pr_touching_the_reader_cannot_land_on_a_skip(monkeypatch):
    monkeypatch.delenv('GITHUB_REPOSITORY', raising=False)
    outputs, runs, needs = pr_needs(['hermes_cli/machine_memory_darwin.py'], fork=True)
    assert outputs['native_memory'] == 'true' and runs is False
    assert run_gate(needs) is False


@pytest.mark.parametrize('event,ref,release,selected', [
    ('push', 'refs/heads/main', False, False),
    ('push', 'refs/heads/main', True, True),
    ('workflow_dispatch', 'refs/heads/main', False, True),
    ('pull_request', 'refs/pull/1/merge', False, True),
])
def test_unknown_diff_fails_open_and_only_main_push_is_masked(event, ref, release, selected):
    assert detect([], context(event, ref, release))['native_memory'] == gha.to_string(selected)
    assert classify([f'docs/{i}.md' for i in range(300)])['native_memory'] is True


def test_classifier_paths_match_the_standalone_workflow_and_it_has_no_unrequired_pr_trigger():
    child = workflow('native-memory-macos.yml')
    assert tuple(child['on']['push']['paths']) == MEMORY_PATHS
    assert getattr(classify_changes, 'NATIVE_MEMORY_PATHS', None) == MEMORY_PATHS
    assert 'pull_request' not in child['on']
    assert child['on']['workflow_call']['inputs']['commit']['default'] == ''
    job = child['jobs']['native-memory']
    assert job['name'] == 'Native memory ABI and no-fork tests'
    assert not job.get('continue-on-error')
    ctx = context()
    ctx['inputs'] = {key: gha.render(value, ctx) for key, value in caller_job()['with'].items()}
    ctx['github']['event']['pull_request']['head']['sha'] = 'pr-head-sha'
    checkout = next(step for step in job['steps'] if workflow_steps.is_checkout(step))
    assert gha.render(checkout['with']['ref'], ctx) == 'pr-head-sha'
    # A required call never queues behind (or supersedes) another run's call.
    other = context()
    other['github']['run_id'] = '124'
    other['inputs'] = ctx['inputs']
    assert gha.render(child['concurrency']['group'], ctx) != gha.render(child['concurrency']['group'], other)
    assert child['concurrency']['cancel-in-progress'] is False
