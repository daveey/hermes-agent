"""Independent exact-head review: supported launch compatibility and classification."""
import json
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.platforms('linux')


def test_ordinary_gateway_launch_survives_shared_home_namespace(tmp_path, monkeypatch):
    from hermes_cli import main, plugins
    import hermes_constants
    monkeypatch.setattr(hermes_constants, 'get_default_hermes_root', lambda: tmp_path)
    runtime = tmp_path / 'serve-producer-v1'
    runtime.mkdir(mode=0o700)
    foreign = dict(identity=dict(boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                                pid=123, start_ticks=456),
                   installation='/opt/hermes', generation='a' * 64, role='gateway',
                   observation='b' * 64, namespace='pid:[1]', guarded=False)
    ledger = runtime / 'participants.json'
    ledger.write_text(json.dumps([foreign]))
    ledger.chmod(0o600)
    for name in ('_apply_safe_mode', '_apply_user_config_bypass', '_guard_noninteractive_user_config'):
        monkeypatch.setattr(main, name, lambda *a: None)
    class DiscoveryReached(BaseException):
        pass
    def reached():
        raise DiscoveryReached()
    monkeypatch.setattr(plugins, 'start_background_plugin_discovery', reached)
    # Dormant producer admission must not disable existing gateway startup.
    with pytest.raises(DiscoveryReached):
        main._prepare_agent_startup(SimpleNamespace(command='gateway', gateway_command='run'))


def test_explicit_hermes_module_is_not_positive_unrelated_evidence():
    from hermes_cli import serve_producer_admission as admission
    from hermes_cli.serve_producer_census import Incarnation, ProcessObservation
    record = ProcessObservation(Incarnation('boot', 123, 456),
        '/opt/other-venv/bin/python', '/tmp',
        ('/opt/other-venv/bin/python', '-m', 'hermes_cli.main', 'serve'), ())
    verdict = admission.classify_process(record, (), 'generation',
        installation='/opt/current-hermes', home='/home/test/.hermes')
    assert verdict.kind != 'unrelated', verdict
