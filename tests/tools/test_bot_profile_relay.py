"""Relay transport is install-wide; sender identity remains profile-scoped."""
import json
import shlex
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from tools import bot_mode_dm, bot_mode_probe, bot_relay
import tui_gateway.server as srv


@pytest.mark.parametrize('target', ['remote', 'remote@cloud'])
def test_named_sender_round_trip_through_default_backend(tmp_path, monkeypatch, capsys, target):
    root = tmp_path / 'state'
    sender = root / 'profiles' / 'sender'
    sender.mkdir(parents=True)
    (sender / 'profile.yaml').write_text('ui_meta:\n  hermes-bots:\n    shape: cloud\n')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    # The representative backend is default, not the sending profile.
    monkeypatch.setenv('HERMES_HOME', str(root))
    remote = dict(profile='remote', handle='remote', connection_id='cloud', title='Connected remote')
    synced = srv._methods['bot_relay.roster.sync'](1, {'agents': [remote]})
    assert synced['result']['count'] == 1
    agent = SimpleNamespace(_session_db=SimpleNamespace(db_path=str(sender / 'state.db')),
                            _session_title_hint='Bot Chat')
    commands = []
    monkeypatch.setattr(bot_mode_dm, '_spawn_delivery', lambda command, *a, **kw:
                        commands.append(command) or json.dumps({'status': 'sent'}))
    result = json.loads(bot_mode_dm.message_agent_tool(target, 'hello', agent=agent))
    assert result == {'status': 'sent'}
    envelopes = srv._methods['bot_relay.outbox.drain'](2, {})['result']['envelopes']
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope['target_connection'] == 'cloud'
    assert envelope['from_profile'] == 'sender'
    assert envelope['message'] == 'Message from 🤖 sender (@sender): hello'
    assert not (sender / 'bot_relay').exists()
    argv = shlex.split(commands[0])[2:]
    assert argv[0] == '--wait-reply'
    assert Path(argv[1]) == root / 'bot_relay' / 'replies' / f"{envelope['id']}.json"
    # Exercise the real waiter entry point against the gateway's real reply write.
    argv[-1] = '10'
    observed_missing = Event()
    original_exists = bot_mode_dm.os.path.exists

    def observe_reply(path):
        exists = original_exists(path)
        if path == argv[1] and not exists:
            observed_missing.set()
        return exists

    monkeypatch.setattr(bot_mode_dm.os.path, 'exists', observe_reply)
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(bot_mode_dm._delivery_main, argv)
        assert observed_missing.wait(timeout=5), 'waiter must start before the reply arrives'
        reply = srv._methods['bot_relay.reply'](3, {'id': envelope['id'], 'reply': 'Remote answered'})
        assert reply['result'] == {'ok': True}
        assert waiting.result(timeout=15) == 0
    assert 'Remote answered' in capsys.readouterr().out
    assert srv._methods['bot_relay.outbox.drain'](4, {})['result']['envelopes'] == []


def test_named_sender_prompt_uses_connection_roster_not_stale_profile_roster(tmp_path):
    root = tmp_path / 'state'
    sender = root / 'profiles' / 'sender'
    sender.mkdir(parents=True)
    (sender / 'profile.yaml').write_text('ui_meta:\n  hermes-bots:\n    shape: cloud\n')
    bot_relay.write_remote_roster(root, [dict(profile='remote', handle='remote', connection_id='cloud',
                                           title='Connected remote')])
    bot_relay.write_remote_roster(sender, [dict(profile='stale', handle='stale', connection_id='old',
                                             title='Stale remote')])
    section = bot_mode_probe.get_bot_mode_protocol_section(sender, force_refresh=True)
    assert 'Connected remote' in section
    assert 'Stale remote' not in section
