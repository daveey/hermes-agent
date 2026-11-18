"""Cached fixed-output paths must not bypass the pnpm hash check."""

import os
from pathlib import Path
import subprocess

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/ci/check_pnpm_deps_hash.sh'


def test_pnpm_restores_without_saving_and_bounds_validation():
    doc = YAML(typ='safe').load((ROOT / '.github/workflows/nix.yml').read_text())
    job = doc['jobs']['pnpm-deps-hash']
    cache = next(s for s in job['steps'] if 'cache-nix-action@' in s.get('uses', ''))
    shared = next(s for s in doc['jobs']['flake-check']['steps']
                  if 'cache-nix-action@' in s.get('uses', ''))
    assert cache['uses'] == shared['uses']
    assert cache['with']['primary-key'] == shared['with']['primary-key']
    assert cache['with']['restore-prefixes-first-match'] == 'nix-${{ runner.os }}-'
    assert cache['with']['save'] is False
    assert cache['with']['purge'] is False
    step = next(s for s in job['steps'] if s.get('name') == 'Check nix/pnpm-deps-hash')
    assert step['run'] == 'timeout --kill-after=30s 15m bash scripts/ci/check_pnpm_deps_hash.sh'
    assert job['timeout-minutes'] == 20
    assert not step.get('continue-on-error', False)


@pytest.mark.parametrize('fail_call', [0, 1, 2])
def test_validation_rebuilds_cached_output_and_reports_mismatch(tmp_path, fail_call):
    fake = tmp_path / 'nix'
    fake.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$RUNNER_TEMP/calls"
count=$(wc -l < "$RUNNER_TEMP/calls")
if [ "$count" = "$FAIL_CALL" ]; then
  printf '       got:    sha256-AAAA=\\n' >&2
  exit 1
fi
''')
    fake.chmod(0o755)
    env = dict(os.environ, PATH=f'{tmp_path}:{os.environ["PATH"]}',
               RUNNER_TEMP=str(tmp_path), FAIL_CALL=str(fail_call))
    result = subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True,
                            text=True, encoding='utf-8', timeout=5, check=False)
    calls = (tmp_path / 'calls').read_text().splitlines()
    assert calls[0] == 'build .#pnpm-deps --no-link --print-build-logs'
    if fail_call != 1:
        assert calls[1] == 'build .#pnpm-deps --no-link --print-build-logs --rebuild'
    assert len(calls) == (1 if fail_call == 1 else 2)
    assert result.returncode == (1 if fail_call else 0)
    if fail_call:
        assert '::error file=nix/pnpm-deps-hash::' in result.stdout
        assert 'sha256-AAAA=' in result.stdout
