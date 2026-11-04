"""The Webapp is the machine owner: a signed-in tab and native Desktop share Group Chats and history.

Hermes is single-user. Sign-in only decides whether a browser may open the Webapp; it never names a
second owner. So a Webapp tab's Group Chats are native Desktop's (and the reverse), and the rooms an
older build stored under the tab's ``auth:v1`` login subject are still the owner's, while another OS
user's subject or a messaging identity stays foreign (gateway.owner_identity).
"""
import json
from types import SimpleNamespace

import pytest


from gateway.owner_identity import host_owner_subject, same_owner
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch

MEMBERS = [{'member_id': 'one', 'profile': 'default', 'handle': 'one'},
           {'member_id': 'two', 'profile': 'two', 'handle': 'two'}]
NATIVE = {'provider': 'local', 'user_id': host_owner_subject(), 'profile_id': None, 'instance_id': 'owner',
          'capabilities': ['session:create', 'session:read', 'session:submit', 'session:control']}


@pytest.fixture
def authority(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / 'profiles' / 'two').mkdir(parents=True)
    (tmp_path / 'config.yaml').write_text(
        'hosted_rooms:\n  profiles:\n    two: ' + str((tmp_path / 'profiles' / 'two').resolve()) + '\n')
    from gateway.session_hosted_service import CanonicalHostedRoomService
    with SessionDB(tmp_path / 'state.db') as db:
        authority = SimpleNamespace(db=db, profile_id=str(tmp_path), instance_id='owner', events={},
                                    epoch=begin_runtime_epoch(db, instance_id='test'))
        authority.hosted_room_service = CanonicalHostedRoomService(authority, None)
        yield authority


async def _call(connection, method, **params):
    reply = await connection.dispatch({'id': 1, 'method': method, 'params': params})
    return reply.get('result', reply.get('error', {}).get('message'))


def _connect(authority, identity):
    from gateway.session_controls import AuthorityConnection
    if identity.get('provider') == 'local':
        identity = {**identity, 'profile_id': authority.profile_id}
    return AuthorityConnection(authority, object(), identity)


@pytest.mark.asyncio
async def test_a_webapp_tab_and_native_desktop_share_one_group_chat_list(authority):
    webapp = _connect(authority, {'user_id': 'admin', 'provider': 'basic'})
    desktop = _connect(authority, NATIVE)
    assert webapp.actor.subject == desktop.actor.subject == host_owner_subject()
    assert await _call(webapp, 'groups.create', room_id='web-room', name='Web', members=MEMBERS) is not None
    assert await _call(desktop, 'groups.create', room_id='desk-room', name='Desk', members=MEMBERS) is not None
    for surface in (webapp, desktop):
        listed = await _call(surface, 'groups.list')
        assert {room['room_id'] for room in listed['rooms']} == {'web-room', 'desk-room'}


@pytest.mark.asyncio
async def test_a_room_an_older_build_stored_under_the_login_stays_the_owners(authority):
    from gateway.session_hosted_service import _OWNER
    desktop = _connect(authority, NATIVE)
    assert await _call(desktop, 'groups.create', room_id='old-web-room', name='Old', members=MEMBERS) is not None
    authority.db._execute_write(lambda conn: conn.execute('UPDATE state_meta SET value=? WHERE key=?',
                                ('auth:v1:["basic","","admin"]', _OWNER + 'old-web-room')))
    assert [r['room_id'] for r in (await _call(desktop, 'groups.list'))['rooms']] == ['old-web-room']
    stranger = _connect(authority, {**NATIVE, 'user_id': 'uid:999999'})
    assert (await _call(stranger, 'groups.list'))['rooms'] == []


OWNER = host_owner_subject()
WEBAPP_LOGIN = 'auth:v1:["basic","","admin"]'


@pytest.mark.parametrize('stamp, owned', [
    ('basic:admin', True), (f'local:{OWNER}', True), ('auth:v1:["self-hosted","https://idp","sub"]', True),
    ('auth:v1:["local","","' + OWNER + '"]', True),
    ('local:uid:999999', False), ('auth:v1:["local","","uid:999999"]', False),
    ('telegram:42', False), ('uid:999999', False), ('nous:account', False), ('auth:v1:["","","x"]', False),
])
def test_only_sign_in_stamps_and_this_machines_account_read_as_the_owners(stamp, owned):
    assert same_owner(stamp, OWNER) is owned
    assert same_owner(stamp, 'uid:999999') is (stamp == 'uid:999999')


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    with SessionDB(tmp_path / 'state.db') as db:
        yield SimpleNamespace(db=db, profile_id=str(tmp_path), sessions={}, _require_admission_open=lambda: None)


def _bind(store, sid, subject, **row):
    from hermes_state_mutation_binding import BINDING_PREFIX
    store.db.create_session(sid, **row)
    binding = json.dumps([store.profile_id, subject], separators=(',', ':'))
    store.db._execute_write(lambda conn: conn.execute(
        'INSERT INTO state_meta(key,value) VALUES(?,?)', (BINDING_PREFIX + sid, binding)))


def test_a_chat_a_webapp_tab_imported_before_the_upgrade_still_opens(store):
    """The sidebar (authorize_history) and the open (mount_history) read a binding by one rule."""
    from gateway.session_contract import SessionActor
    from gateway.session_history_mount import mount_history
    from hermes_state_mutation_binding import authorize_history
    caps = frozenset({'session:create', 'session:read'})
    owner, stranger = (SessionActor(s, store.profile_id, caps, 't') for s in (OWNER, 'uid:999999'))
    _bind(store, 'imported-web', WEBAPP_LOGIN, source='webui')
    with store.db._read_ctx() as conn:
        assert authorize_history(conn, owner, 'imported-web') is True
    assert mount_history(store, owner, 'imported-web') is True
    store.sessions.clear()
    assert mount_history(store, stranger, 'imported-web') is False


def test_an_old_chat_a_webapp_tab_edited_is_adopted_by_the_owner(store):
    from hermes_state_local_migration import bind_legacy_target
    from hermes_state_runtime import RuntimeStoreError
    _bind(store, 'legacy-web', WEBAPP_LOGIN, source='cli')

    def adopt(subject):
        receipt = {'legacy_session_id': 'legacy-web', 'session_id': 'legacy-web', 'principal_id': subject,
                   'profile_id': store.profile_id, 'route': 'agent:main:local:dm:legacy-web',
                   'entry': {'origin': {'platform': 'local', 'chat_id': 'legacy-web', 'user_id': subject}}}
        store.db._execute_write(lambda conn: bind_legacy_target(store.db, conn, receipt))
    with pytest.raises(RuntimeStoreError, match='permission_denied'):
        adopt('uid:999999')
    adopt(OWNER)
    assert store.db.get_session('legacy-web')['user_id'] == OWNER


def _ssh_desktop_request(tmp_path):
    """A REST request as an SSH-served Desktop presents it: the loopback ``serve --isolated`` binds the
    validated token file's ownership id as ``provider='desktop-ssh'`` (web_server._ssh_bootstrap_session)."""
    from hermes_cli.dashboard_auth.base import Session
    session = Session(user_id='a' * 32, provider='desktop-ssh', email='', display_name='', org_id='',
                      expires_at=0, access_token='', refresh_token='')
    return SimpleNamespace(state=SimpleNamespace(native_http_principal=None, session=session),
                           app=SimpleNamespace(state=SimpleNamespace(session_authority=None, gateway_runner=None)))


def test_another_macs_ssh_desktop_edits_the_owners_desktop_chat(store, tmp_path):
    """Archive/pin/rename from another Mac's Desktop over SSH is the owner (it logged in over SSH as this
    host's OS user), so the owner's routeless desktop chat (user_id = the owner) is its to edit (#archive 403)."""
    from hermes_cli.web_server_sessions import _session_mutation_context
    from hermes_state_mutation_binding import authorize_history
    _authority, actor = _session_mutation_context(_ssh_desktop_request(tmp_path), None)
    assert actor.subject == OWNER
    store.db.create_session('hub-desktop-chat', source='desktop', user_id=OWNER)
    with store.db._read_ctx() as conn:
        assert authorize_history(conn, actor, 'hub-desktop-chat') is True


@pytest.mark.parametrize('stamp', ['desktop-ssh:' + 'a' * 32, 'auth:v1:["desktop-ssh","","' + 'a' * 32 + '"]'])
def test_a_stamp_an_ssh_desktop_stored_reads_as_the_owners(stamp):
    assert same_owner(stamp, OWNER) is True
    assert same_owner(stamp, 'uid:999999') is False
