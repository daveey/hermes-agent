"""Install is explicit, root-only and never discovers a process allowlist."""
import json

import pytest

from hermes_cli import worker_drain_baseline as baseline
from hermes_cli import worker_drain_baseline_cli as cli
from hermes_cli import worker_drain_fence as fence

pytestmark = pytest.mark.platforms('linux')


def test_capture_command_calls_strict_installer(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(baseline, 'install', lambda: calls.append('capture') or {'version': 2})
    assert cli.main(['capture']) == 0
    assert calls == ['capture']
    assert json.loads(capsys.readouterr().out) == {'version': 2}


def test_refusal_has_nonzero_status_without_fake_observation(monkeypatch, capsys):
    def refuse():
        raise baseline.CaptureRefused('undeclared live process')
    monkeypatch.setattr(baseline, 'install', refuse)
    assert cli.main(['capture']) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert 'undeclared live process' in captured.err


def test_prepare_binds_current_boot_and_post_activation_ticks(monkeypatch):
    manifest = {'version': 1, 'units': [], 'runtime': {'installed_start_ticks': 0}}
    calls = []
    monkeypatch.setattr(cli.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(baseline, 'load_manifest', lambda path: manifest)
    monkeypatch.setattr(baseline, 'write_baseline', lambda path, doc: calls.append((path, doc)))
    monkeypatch.setattr(fence, 'load_activation', dict)
    monkeypatch.setattr(fence, 'current_identity', lambda act: fence.Identity('i-12345678', 'boot-a', 'epoch-a'))
    from hermes_cli import worker_drain_capability as cap
    monkeypatch.setattr(cap, 'verified_contract', lambda doc: {'digest': 'verified'})
    monkeypatch.setattr(cli, 'boot_ticks', lambda: 1000)
    doc = cli.prepare('image-manifest')
    assert doc['runtime']['installed_boot_id'] == 'boot-a'
    assert doc['runtime']['installed_start_ticks'] == 1000
    assert doc['runtime']['activation_start_ticks'] == 1000
    assert calls == [(baseline.MANIFEST_PATH, doc)]
    assert manifest['runtime'] == {'installed_start_ticks': 0}  # caller template is not mutated


def test_prepare_requires_activation_and_root(monkeypatch):
    monkeypatch.setattr(cli.os, 'geteuid', lambda: 1000)
    with pytest.raises(baseline.CaptureRefused, match='root'):
        cli.prepare('image')
    monkeypatch.setattr(cli.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(fence, 'load_activation', lambda: None)
    with pytest.raises(baseline.CaptureRefused, match='activat'):
        cli.prepare('image')
