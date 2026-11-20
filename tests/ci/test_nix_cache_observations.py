"""Exercise cache summaries and the root budget at their CI entry points."""

import os
from pathlib import Path
import subprocess

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(('event', 'ref', 'hit', 'copies', 'fetches', 'warn'), [
    ('push', 'refs/heads/main', 'true', 368, 5, False),
    ('push', 'refs/heads/main', 'true', 0, 11, True),
    ('push', 'refs/heads/main', 'true', 368, 10, False),
    ('push', 'refs/heads/main', 'true', 93, 239, True),
    ('push', 'refs/heads/main', 'false', 93, 239, False),
    ('pull_request', 'refs/heads/main', 'true', 0, 11, False),
    ('push', 'refs/heads/topic', 'true', 0, 11, False),
])
def test_summary_counts_real_log_lines(tmp_path, event, ref, hit, copies, fetches, warn):
    log = tmp_path / 'build.log'
    log.write_text(
        "copying path '/nix/store/a' from 'https://cache.nixos.org'...\n" * copies
        + 'source> trying https://example.com/source.tar.gz\n' * fetches
        + 'echo "copying path /nix/store/quoted"\n'
    )
    summary = tmp_path / 'summary'
    env = dict(os.environ, GITHUB_EVENT_NAME=event, GITHUB_REF=ref,
               NIX_CACHE_HIT=hit, GITHUB_STEP_SUMMARY=str(summary))
    result = subprocess.run(
        ['python3', str(ROOT / 'scripts/ci/nix_cache_summary.py'), str(log), str(tmp_path / 'missing.log')],
        env=env, capture_output=True, text=True, encoding='utf-8', check=False,
    )
    assert result.returncode == 0, result.stderr
    assert f'Paths copied from substituters: {copies}' in summary.read_text()
    assert f'HTTPS source fetches: {fetches}' in summary.read_text()
    assert ('::warning::' in result.stdout) is warn


@pytest.mark.parametrize('shape', ['list', 'dict'])
@pytest.mark.parametrize(('size', 'success'), [(1024, True), (5368709120, True), (5368709121, False)])
def test_root_budget_is_enforced_after_realization(tmp_path, size, success, shape):
    nix = tmp_path / 'nix'
    nix.write_text('''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$RUNNER_TEMP/calls"
if [ "$1" = build ]; then
  while [ "$1" != --out-link ]; do shift; done
  ln -s /nix/store/stable-tools "$2"
else
  if [ "$SHAPE" = dict ]; then
    printf '{"/nix/store/stable-tools":{"closureSize":%s}}\\n' "$SIZE"
  else
    printf '[{"path":"/nix/store/stable-tools","closureSize":%s}]\\n' "$SIZE"
  fi
fi
''')
    nix.chmod(0o755)
    summary = tmp_path / 'summary'
    env = dict(os.environ, PATH=f'{tmp_path}:{os.environ["PATH"]}',
               RUNNER_TEMP=str(tmp_path), GITHUB_STEP_SUMMARY=str(summary), SIZE=str(size), SHAPE=shape)
    result = subprocess.run(
        ['bash', str(ROOT / 'scripts/ci/root_nix_cache.sh')], env=env,
        capture_output=True, text=True, encoding='utf-8', check=False,
    )
    assert (result.returncode == 0) is success, result.stderr
    assert (tmp_path / 'nix-cache-roots/stable').is_symlink()
    assert 'build .#ci-cache --out-link' in (tmp_path / 'calls').read_text()
    assert str(size) in summary.read_text()
    assert ('::error::' in result.stdout) is not success


@pytest.mark.parametrize(('mode', 'expected'), [('over-budget', 1), ('build-fails', 42), ('within-budget', 0)])
def test_root_workflow_propagates_failures(tmp_path, mode, expected):
    doc = YAML(typ='safe').load((ROOT / '.github/workflows/nix.yml').read_text())
    steps = doc['jobs']['flake-check']['steps']
    root = next(s for s in steps if s.get('name') == 'Root stable Nix outputs before cache GC')
    assert 'if' not in root
    assert not root.get('continue-on-error', False)
    nix = tmp_path / 'nix'
    nix.write_text('''#!/usr/bin/env bash
if [ "$1" = build ]; then
  if [ "$MODE" = build-fails ]; then echo build-failed >&2; exit 42; fi
  while [ "$1" != --out-link ]; do shift; done
  ln -s /nix/store/stable-tools "$2"
else
  if [ "$MODE" = over-budget ]; then size=5368709121; else size=1024; fi
  printf '[{"closureSize":%s}]\\n' "$size"
fi
''')
    nix.chmod(0o755)
    result = subprocess.run(
        ['bash', '-e', '-c', root['run']], cwd=ROOT,
        env=dict(os.environ, PATH=f'{tmp_path}:{os.environ["PATH"]}', MODE=mode,
                 RUNNER_TEMP=str(tmp_path), GITHUB_STEP_SUMMARY=str(tmp_path / 'summary')),
        capture_output=True, text=True, encoding='utf-8', check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    log = (tmp_path / 'nix-cache-roots.log').read_text()
    assert ('::error::' in log) is (mode == 'over-budget')
    if mode == 'build-fails':
        assert 'build-failed' in log


def test_cache_roots_only_tools_used_by_checks():
    packages = (ROOT / 'nix/packages.nix').read_text()
    roots = packages.split('lib.attrVals [', 1)[1].split(']', 1)[0]
    assert roots.split() == ['"pnpm"', '"ripgrep"', '"ffmpeg"', '"node"', '"python"', '"uv"']


def test_workflow_tee_retains_failure_and_summary_is_always_run(tmp_path):
    doc = YAML(typ='safe').load((ROOT / '.github/workflows/nix.yml').read_text())
    steps = doc['jobs']['flake-check']['steps']
    build = next(s for s in steps if s.get('name') == 'nix flake check')
    summary = next(s for s in steps if s.get('name') == 'Summarize Nix cache effectiveness')
    assert summary['if'] == 'always()'
    assert summary['env']['NIX_CACHE_HIT'] == '${{ steps.nix-cache.outputs.hit-primary-key }}'
    assert any(s.get('id') == 'nix-cache' for s in steps)
    nix = tmp_path / 'nix'
    nix.write_text('#!/usr/bin/env bash\necho "source> trying https://example.com/file" >&2\nexit 42\n')
    nix.chmod(0o755)
    command = build['run']
    start = command.index('${{')
    end = command.index('}}', start) + 2
    command = command[:start] + '.' + command[end:]
    result = subprocess.run(
        ['bash', '-e', '-c', command],
        env=dict(os.environ, PATH=f'{tmp_path}:{os.environ["PATH"]}', RUNNER_TEMP=str(tmp_path)),
        capture_output=True, text=True, encoding='utf-8', check=False,
    )
    assert result.returncode == 42
    assert 'source> trying https://' in (tmp_path / 'nix-flake-check.log').read_text()
