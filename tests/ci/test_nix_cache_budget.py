"""Nix caching must not require cache-delete privileges or hide build timeouts."""

from pathlib import Path
import subprocess

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]


def workflow(name):
    return YAML(typ='safe').load((ROOT / '.github/workflows' / name).read_text())


def test_cache_save_does_not_purge_with_read_only_token():
    doc = workflow('nix.yml')
    job = doc['jobs']['flake-check']
    cache = next(s for s in job['steps'] if 'cache-nix-action@' in s.get('uses', ''))
    assert cache['with'].get('purge', False) is False
    assert doc['permissions'] == {'contents': 'read'}
    assert 'permissions' not in job
    assert cache['with']['gc-max-store-size-linux'] == '5G'


def test_nix_timeout_is_a_command_failure(tmp_path):
    job = workflow('nix.yml')['jobs']['flake-check']
    step = next(s for s in job['steps'] if s.get('name') == 'nix flake check')
    command = step['run']
    assert 'timeout --kill-after=30s 40m nix flake check' in command
    assert job['timeout-minutes'] == 60
    assert not step.get('continue-on-error', False)
    # Execute the same timeout prefix with a deliberately stalled child. GNU
    # timeout's 124 is a failing step, unlike the runner's cancelled job deadline.
    prefix = command.split('nix flake check')[0].replace('40m', '0.1s')
    result = subprocess.run(
        ['bash', '-e', '-c', prefix + ' bash -c "read -r -t 10 < <(tail -f /dev/null)"'],
        capture_output=True, timeout=5, check=False,
    )
    assert result.returncode == 124


def test_fork_docker_cache_cannot_compete_with_nix():
    doc = workflow('docker.yml')
    cache_steps = [s['with'] for job in doc['jobs'].values()
                   for s in job.get('steps', []) if 'cache-from' in s.get('with', {})]
    assert len(cache_steps) == 2
    for settings in cache_steps:
        # Empty inputs disable both read and write. Reads alone refresh old
        # buildkit blobs' LRU timestamps and can still evict the Nix archive.
        for direction in ('cache-from', 'cache-to'):
            value = settings[direction]
            assert value.startswith("${{ github.repository != 'softmax-universe/daveey-hermes' && ")
            assert value.endswith(" || '' }}")
        assert 'mode=min,' in settings['cache-to']
    assert doc['permissions'] == {'contents': 'read'}


def test_docker_exports_final_layers_only():
    doc = workflow('docker.yml')
    exports = [s['with']['cache-to'] for job in doc['jobs'].values()
               for s in job.get('steps', []) if 'cache-to' in s.get('with', {})]
    assert len(exports) == 2
    assert all('mode=min,' in export and 'mode=max' not in export for export in exports)
