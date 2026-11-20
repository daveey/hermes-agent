"""Selected recovery replaces one owned row, never adds or guesses an identity."""
import base64
import json

import pytest

from hermes_cli import auth


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / 'auth.json'
    monkeypatch.setattr(auth, '_auth_file_path', lambda: path)
    row = dict(id='selected', source='manual:hermes_pkce', auth_type='oauth',
               label='Example', priority=3, access_token='fixture-old',
               refresh_token='fixture-refresh', last_status='dead',
               last_error_code=401, last_error_reason='auth', last_error_message='expired', last_error_reset_at=123)
    payload = dict(providers={}, credential_pool={'anthropic': [row]})
    path.write_text(json.dumps(payload))
    return path


def test_replacement_preserves_selected_row_without_duplicate(store):
    from hermes_cli.auth_reauth_target import commit_reauth, prepare_reauth
    target = prepare_reauth('anthropic', 'selected')
    result = commit_reauth(target, {'access_token': 'fixture-new', 'refresh_token': 'fixture-new-refresh'},
                           allow_unverified_identity=True)
    rows = json.loads(store.read_text())['credential_pool']['anthropic']
    assert len(rows) == 1
    assert rows[0]['id'] == 'selected'
    assert rows[0]['label'] == 'Example'
    assert rows[0]['priority'] == 3
    assert rows[0]['access_token'] == 'fixture-new'
    assert rows[0].get('last_status') is None
    assert not any(key.startswith('last_error') for key in rows[0])
    assert result.identity_verified is False
    assert result.credential_id == 'selected'


@pytest.mark.parametrize('change', ['delete', 'rotate', 'disable', 'source', 'other_home'])
def test_changed_target_refuses_without_write(store, monkeypatch, change):
    from hermes_cli.auth_reauth_target import ReauthConflict, commit_reauth, prepare_reauth
    from agent import credential_pool_disabled as disabled
    target = prepare_reauth('anthropic', 'selected')
    data = json.loads(store.read_text())
    row = data['credential_pool']['anthropic'][0]
    mutations = {
        'delete': lambda: data['credential_pool'].update(anthropic=[]),
        'rotate': lambda: row.update(access_token='fixture-peer'),
        'source': lambda: row.update(source='claude_code'),
        'disable': lambda: monkeypatch.setattr(disabled, 'is_credential_disabled', lambda *args: True),
        'other_home': lambda: monkeypatch.setattr(auth, '_auth_file_path', lambda: store.parent / 'other.json'),
    }
    mutations[change]()
    store.write_text(json.dumps(data))
    before = store.read_bytes()
    with pytest.raises(ReauthConflict):
        commit_reauth(target, {'access_token': 'fixture-new', 'refresh_token': 'fixture-new-refresh'},
                      allow_unverified_identity=True)
    assert store.read_bytes() == before


@pytest.mark.parametrize('source', ['claude_code', 'env:EXAMPLE_TOKEN', 'device_code', 'manual:unknown'])
def test_unowned_or_unknown_sources_are_not_copied(store, source):
    from hermes_cli.auth_reauth_target import ReauthConflict, prepare_reauth
    data = json.loads(store.read_text())
    data['credential_pool']['anthropic'][0]['source'] = source
    store.write_text(json.dumps(data))
    before = store.read_bytes()
    with pytest.raises(ReauthConflict):
        prepare_reauth('anthropic', 'selected')
    assert store.read_bytes() == before


def test_unknown_identity_requires_explicit_replacement(store):
    from hermes_cli.auth_reauth_target import ReauthConflict, commit_reauth, prepare_reauth
    target = prepare_reauth('anthropic', 'selected')
    before = store.read_bytes()
    with pytest.raises(ReauthConflict):
        commit_reauth(target, {'access_token': 'fixture-new', 'refresh_token': 'fixture-new-refresh'})
    assert store.read_bytes() == before


def test_other_rows_and_metadata_survive(store):
    from hermes_cli.auth_reauth_target import commit_reauth, prepare_reauth
    target = prepare_reauth('anthropic', 'selected')
    data = json.loads(store.read_text())
    other = dict(id='other', source='manual:hermes_pkce', access_token='fixture-peer')
    data['credential_pool']['anthropic'].append(other)
    data['unrelated'] = {'keep': True}
    store.write_text(json.dumps(data))
    commit_reauth(target, {'access_token': 'fixture-new', 'refresh_token': 'fixture-new-refresh'},
                  allow_unverified_identity=True)
    saved = json.loads(store.read_text())
    assert saved['credential_pool']['anthropic'][1] == other
    assert saved['unrelated'] == {'keep': True}


def test_target_repr_does_not_disclose_credentials(store):
    from hermes_cli.auth_reauth_target import prepare_reauth
    target = prepare_reauth('anthropic', 'selected')
    assert 'fixture-old' not in repr(target)
    assert 'fixture-refresh' not in repr(target)


def _jwt(account, subject, nonce):
    claims = {'https://api.openai.com/auth': {'chatgpt_account_id': account}, 'sub': subject, 'nonce': nonce}
    return 'fixture.' + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=') + '.fixture'


@pytest.mark.parametrize('account,subject,accepted', [
    ('workspace-a', 'subject-a', True), ('workspace-b', 'subject-a', False),
    ('workspace-a', 'subject-b', False), ('', 'subject-a', False)])
def test_codex_principal_and_workspace_must_both_match(store, account, subject, accepted):
    from hermes_cli.auth_reauth_target import ReauthConflict, commit_reauth, prepare_reauth
    row = json.loads(store.read_text())['credential_pool']['anthropic'][0]
    row.update(source='manual:device_code', access_token=_jwt('workspace-a', 'subject-a', 'old'))
    store.write_text(json.dumps({'providers': {}, 'credential_pool': {'openai-codex': [row]}}))
    target = prepare_reauth('openai-codex', 'selected')
    grant = {'access_token': _jwt(account, subject, 'new'), 'refresh_token': 'fixture-new-refresh'}
    before = store.read_bytes()
    if accepted:
        assert commit_reauth(target, grant).identity_verified is True
        saved = json.loads(store.read_text())['credential_pool']['openai-codex']
        assert len(saved) == 1 and saved[0]['id'] == 'selected'
    else:
        with pytest.raises(ReauthConflict):
            commit_reauth(target, grant, allow_unverified_identity=True)
        assert store.read_bytes() == before


@pytest.mark.parametrize('grant', [{}, {'access_token': 'fixture-new'},
                                  {'access_token': '', 'refresh_token': 'fixture-refresh'}])
def test_incomplete_grant_does_not_write(store, grant):
    from hermes_cli.auth_reauth_target import ReauthConflict, commit_reauth, prepare_reauth
    target = prepare_reauth('anthropic', 'selected')
    before = store.read_bytes()
    with pytest.raises(ReauthConflict):
        commit_reauth(target, grant, allow_unverified_identity=True)
    assert store.read_bytes() == before


def test_incoming_disabled_grant_is_refused_before_save(store, monkeypatch):
    from hermes_cli.auth_reauth_target import ReauthConflict, commit_reauth, prepare_reauth
    from agent import credential_pool_disabled as disabled
    target = prepare_reauth('anthropic', 'selected')
    monkeypatch.setattr(disabled, 'is_credential_disabled',
                        lambda provider, row: row.access_token == 'fixture-disabled')
    before = store.read_bytes()
    with pytest.raises(ReauthConflict):
        commit_reauth(target, {'access_token': 'fixture-disabled', 'refresh_token': 'fixture-new-refresh'},
                      allow_unverified_identity=True)
    assert store.read_bytes() == before


def test_committed_target_is_single_use_and_grant_cannot_edit_metadata(store):
    from hermes_cli.auth_reauth_target import ReauthConflict, commit_reauth, prepare_reauth
    target = prepare_reauth('anthropic', 'selected')
    grant = dict(access_token='fixture-new', refresh_token='fixture-refresh-new',
                 id='injected', label='injected', source='env:INJECTED', priority=0)
    commit_reauth(target, grant, allow_unverified_identity=True)
    before = store.read_bytes()
    with pytest.raises(ReauthConflict):
        commit_reauth(target, grant, allow_unverified_identity=True)
    assert store.read_bytes() == before
    row = json.loads(before)['credential_pool']['anthropic'][0]
    assert (row['id'], row['label'], row['source'], row['priority']) == (
        'selected', 'Example', 'manual:hermes_pkce', 3)
