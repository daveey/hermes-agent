"""Real CLI dispatch and persistence with fixture-only provider exchange."""
import argparse
import json
import sys

import pytest

from hermes_cli import auth
from hermes_cli.auth_commands import auth_command
from hermes_cli.subcommands.auth import build_auth_parser


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    path = tmp_path / 'auth.json'
    monkeypatch.setattr(auth, '_auth_file_path', lambda: path)
    path.write_text(json.dumps({'providers': {}, 'credential_pool': {'anthropic': [dict(
        id='selected', source='manual:hermes_pkce', auth_type='oauth', label='Example',
        priority=2, access_token='fixture-old', refresh_token='fixture-old-refresh', last_status='dead')]}}))
    parser = argparse.ArgumentParser()
    build_auth_parser(parser.add_subparsers(), cmd_auth=auth_command)
    args = parser.parse_args(['auth', 'reauth', 'anthropic', 'selected'])
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    return path, args


def test_cli_dispatch_replaces_selected_grant_without_singleton(recovery, monkeypatch, capsys):
    from agent import anthropic_credentials as anthropic
    from hermes_cli import auth_reauth_command as command
    path, args = recovery
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda prompt: 'replace')
    monkeypatch.setattr(command.getpass, 'getpass', lambda prompt: 'fixture-code#fixture-state')
    monkeypatch.setattr(anthropic.secrets, 'token_urlsafe', lambda n: 'fixture-state')
    monkeypatch.setattr(auth, '_can_open_graphical_browser', lambda: False)
    calls = []

    def exchange(data, **kwargs):
        calls.append(json.loads(data))
        return {'access_token': 'fixture-new', 'refresh_token': 'fixture-new-refresh', 'expires_in': 3600}

    monkeypatch.setattr(anthropic, '_post_oauth_token', exchange)
    args.func(args)
    saved = json.loads(path.read_text())
    assert len(saved['credential_pool']['anthropic']) == 1
    assert saved['credential_pool']['anthropic'][0]['access_token'] == 'fixture-new'
    assert saved['credential_pool']['anthropic'][0]['id'] == 'selected'
    assert saved['providers'] == {}
    assert calls[0]['code'] == 'fixture-code'
    output = capsys.readouterr().out
    assert 'identity remains unverified' in output
    assert 'fixture-code' not in output
    assert 'fixture-new' not in output
    assert 'Reconnected' not in output


@pytest.mark.parametrize('mode', ['cancel', 'no_tty', 'peer_delete'])
def test_cli_refusal_does_not_save(recovery, monkeypatch, mode):
    from agent import anthropic_credentials as anthropic
    path, args = recovery
    monkeypatch.setattr('builtins.input', lambda prompt: 'cancel' if mode == 'cancel' else 'replace')
    calls = []

    def login(**kwargs):
        calls.append(True)
        data = json.loads(path.read_text())
        data['credential_pool']['anthropic'] = []
        path.write_text(json.dumps(data))
        return {'access_token': 'fixture-new', 'refresh_token': 'fixture-new-refresh'}

    monkeypatch.setattr(anthropic, 'run_hermes_oauth_login_pure', login)
    if mode == 'no_tty':
        monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    before = path.read_bytes()
    if mode == 'cancel':
        args.func(args)
    else:
        with pytest.raises(SystemExit):
            args.func(args)
    if mode != 'peer_delete':
        assert path.read_bytes() == before and calls == []
    else:
        assert json.loads(path.read_text())['credential_pool']['anthropic'] == []


@pytest.mark.parametrize('saved_first', [True, False])
def test_interrupt_during_commit_never_claims_unchanged(recovery, monkeypatch, capsys, saved_first):
    from agent import anthropic_credentials as anthropic
    path, args = recovery
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda prompt: 'replace')
    monkeypatch.setattr(anthropic, 'run_hermes_oauth_login_pure',
                        lambda **kwargs: {'access_token': 'fixture-new', 'refresh_token': 'fixture-new-refresh'})
    save = auth._save_auth_store

    def interrupted(*args, **kwargs):
        if saved_first:
            save(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(auth, '_save_auth_store', interrupted)
    args.func(args)
    output = capsys.readouterr().out
    assert 'state is unverified' in output
    assert 'no credential was changed' not in output
    expected = 'fixture-new' if saved_first else 'fixture-old'
    assert json.loads(path.read_text())['credential_pool']['anthropic'][0]['access_token'] == expected


@pytest.mark.parametrize('http_error', [True, False])
def test_secure_exchange_error_does_not_leak_to_output_or_debug(recovery, monkeypatch, capsys, caplog, http_error):
    import io
    import logging
    import urllib.error
    import urllib.request
    from agent import anthropic_credentials as anthropic
    from hermes_cli import auth_reauth_command as command
    path, args = recovery
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda prompt: 'replace')
    monkeypatch.setattr(command.getpass, 'getpass', lambda prompt: 'fixture-code#fixture-state')
    monkeypatch.setattr(anthropic.secrets, 'token_urlsafe', lambda n: 'fixture-state')
    monkeypatch.setattr(auth, '_can_open_graphical_browser', lambda: False)
    caplog.set_level(logging.DEBUG, logger=anthropic.logger.name)
    sentinel = 'fixture-sensitive-response'

    def fail(*args, **kwargs):
        if http_error:
            body = json.dumps({'error': 'invalid_grant', 'error_description': sentinel}).encode()
            raise urllib.error.HTTPError('https://example.com', 400, sentinel, {}, io.BytesIO(body))
        raise OSError(sentinel)

    monkeypatch.setattr(urllib.request, 'urlopen', fail)
    before = path.read_bytes()
    args.func(args)
    assert path.read_bytes() == before
    assert sentinel not in caplog.text + capsys.readouterr().out


def test_no_echo_fallback_is_refused(monkeypatch):
    import warnings
    from hermes_cli import auth_reauth_command as command

    def unavailable(prompt):
        warnings.warn('echo unavailable', command.getpass.GetPassWarning)
        pytest.fail('warning must raise before reading an unmasked secret')

    monkeypatch.setattr(command.getpass, 'getpass', unavailable)
    with pytest.raises(command.getpass.GetPassWarning):
        command._secret_code('Code: ')
