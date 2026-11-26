"""An unusable store can recover while the gateway stays alive; fatal config cannot auto-retry."""
from unittest.mock import patch

import pytest

from tests.gateway.test_multiplex_hot_serve import _mkprofile, _runner


@pytest.mark.asyncio
async def test_watcher_retries_recovered_store_but_not_fatal_config(tmp_path, monkeypatch):
    runner, home = _runner(tmp_path, monkeypatch)
    calls = []
    recovery = False

    async def serve(name, profile_home):
        calls.append(name)
        if not recovery:
            return ('session store unusable: DeletedWalGenerationError' if name == 'recovering'
                    else 'fatal configuration: closed credentials')
        return None

    with patch('hermes_cli.profiles.get_active_profile_name', return_value='default'):
        await runner._start_secondary_profile_adapters()
        _mkprofile(home, 'recovering', 'DISCORD_BOT_TOKEN=recovery-token\n')
        _mkprofile(home, 'fatal', 'DISCORD_BOT_TOKEN=fatal-token\n')
        runner._serve_profile_runtime = serve
        first = await runner.reconcile_served_profiles(reason='control-socket')
        assert set(first['parked']) == {'recovering', 'fatal'}
        recovery = True
        second = await runner.reconcile_served_profiles(reason='watcher')
        assert second['added'] == ['recovering']
        assert calls.count('recovering') == 2
        assert calls.count('fatal') == 1
        assert 'fatal' in runner.session_runtime_descriptor['parked_profiles']
        assert 'recovering' not in runner.session_runtime_descriptor['parked_profiles']
