"""Ordinary launch survives a shared-home foreign PID-namespace receipt (container topology)."""
import json
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
    with pytest.raises(DiscoveryReached):
        main._prepare_agent_startup(SimpleNamespace(command='gateway', gateway_command='run'))
