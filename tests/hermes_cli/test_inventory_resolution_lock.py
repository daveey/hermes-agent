"""Fresh filesystem resolution must not serialize unrelated audit ingress."""
import asyncio
import os
import threading
from pathlib import Path
from types import ModuleType

import pytest

from tests.hermes_cli.test_inventory_diagnostics_lock_order import ledger_fixture


def test_import_audit_progresses_while_inventory_resolves(tmp_path, monkeypatch):
    ledger = ledger_fixture(tmp_path)
    source = tmp_path / 'module.py'
    source.write_text('value = 1\n')
    module = ModuleType('inventory_fixture')
    module.__file__ = str(source)
    ledger._module_snapshot = lambda: [('inventory_fixture', module)]
    entered, release, audited = (threading.Event() for _ in range(3))
    original = Path.resolve
    errors, observations = [], []

    def resolve(path, *args, **kwargs):
        if path == source:
            entered.set()
            if not release.wait(5):
                raise AssertionError('fixture resolution release missing')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', resolve)

    def inventory():
        try:
            observations.append(ledger.integrity_observation())
        except BaseException as exc:
            errors.append(exc)

    def audit():
        ledger._audit('import', ('other_fixture', None))
        audited.set()

    reader = threading.Thread(target=inventory)
    ingress = threading.Thread(target=audit)
    reader.start()
    try:
        assert entered.wait(3), 'inventory did not reach fresh resolver'
        ingress.start()
        progressed = audited.wait(0.5)
    finally:
        release.set()
        reader.join(5)
        if ingress.ident is not None:
            ingress.join(5)
    assert not reader.is_alive() and not ingress.is_alive()
    assert not errors
    assert progressed, 'unrelated audit ingress queued behind filesystem resolution'
    assert len(observations) == 1 and observations[0]['unknowns'] == ()
    assert ledger._generation >= 1


@pytest.mark.parametrize('change', ['filename', 'replacement', 'addition', 'removal'])
def test_changed_snapshot_never_uses_stale_prepared_path(tmp_path, monkeypatch, change):
    ledger = ledger_fixture(tmp_path)
    first, second = tmp_path / 'first.py', tmp_path / 'second.py'
    first.write_text('first = 1\n')
    second.write_text('second = 2\n')
    a, b = ModuleType('a'), ModuleType('b')
    a.__file__, b.__file__ = str(first), str(second)
    modules = [('a', a)]
    monkeypatch.setattr(ledger, '_module_snapshot', lambda: tuple(modules))
    real = Path.resolve
    classified = []

    def resolve(path, *args, **kwargs):
        result = real(path, *args, **kwargs)
        if path == first:
            assert not ledger._lock._is_owned()
            if change == 'filename':
                a.__file__ = str(second)
            elif change == 'replacement':
                modules[:] = [('a', b)]
            elif change == 'addition':
                modules.append(('b', b))
            else:
                modules.clear()
        return result

    def classify(name, filename, path, owner):
        assert ledger._lock._is_owned()
        classified.append((name, filename, path))

    monkeypatch.setattr(Path, 'resolve', resolve)
    monkeypatch.setattr(ledger, '_classify_module_file', classify)
    ledger.integrity_observation()
    expected = {'filename': [('a', str(second), second)],
                'replacement': [('a', str(second), second)],
                'addition': [('a', str(first), first), ('b', str(second), second)],
                'removal': []}
    assert classified == expected[change]


@pytest.mark.parametrize('entry', ['integrity_observation', 'unknowns', 'partition'])
def test_every_observation_freshly_resolves_symlinks(tmp_path, monkeypatch, entry):
    ledger = ledger_fixture(tmp_path)
    one, two, link = tmp_path / 'one.py', tmp_path / 'two.py', tmp_path / 'link.py'
    one.write_text('one = 1\n')
    two.write_text('two = 2\n')
    link.symlink_to(one)
    module = ModuleType('linked')
    module.__file__ = str(link)
    monkeypatch.setattr(ledger, '_module_snapshot', lambda: (('linked', module),))
    classified = []
    monkeypatch.setattr(ledger, '_classify_module_file', lambda n, f, p, o: classified.append(p))

    def observe():
        if entry == 'partition':
            return ledger.quiet_unknown_partition(inventory=True)
        return getattr(ledger, entry)()

    observe()
    link.unlink()
    link.symlink_to(two)
    observe()
    assert classified == [one, two]


def test_nested_reader_retains_held_lock_and_fresh_resolution(tmp_path, monkeypatch):
    ledger = ledger_fixture(tmp_path)
    module = ModuleType('nested')
    module.__file__ = str(tmp_path / 'nested.py')
    monkeypatch.setattr(ledger, '_module_snapshot', lambda: (('nested', module),))
    real = Path.resolve
    ownership = []

    def resolve(path, *args, **kwargs):
        ownership.append(ledger._lock._is_owned())
        return real(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', resolve)
    with ledger._lock:
        assert ledger.integrity_observation()['unknowns'] == ()
        assert ledger._lock._is_owned()
    assert ownership == [True]


def test_failed_preparation_never_returns_observation_and_releases_lock(tmp_path, monkeypatch):
    ledger = ledger_fixture(tmp_path)
    module = ModuleType('broken')
    module.__file__ = str(tmp_path / 'broken.py')
    monkeypatch.setattr(ledger, '_module_snapshot', lambda: (('broken', module),))
    original = OSError('controlled resolver refusal')

    def resolve(*args, **kwargs):
        raise original

    monkeypatch.setattr(Path, 'resolve', resolve)
    with pytest.raises(OSError) as raised:
        ledger.integrity_observation()
    assert raised.value is original
    assert not ledger._lock._is_owned()
    assert ledger._modules == {}


@pytest.mark.skipif(os.name == 'nt', reason='POSIX authenticated local socket proof')
def test_native_ticket_socket_progresses_during_fresh_resolution(tmp_path, monkeypatch):
    from gateway.control_socket import GatewayControlServer, query_gateway_control
    from gateway.runtime_bootstrap import TicketStore
    from gateway import runtime_bootstrap

    ledger = ledger_fixture(tmp_path)
    module = ModuleType('slow_inventory_module')
    source = tmp_path / 'slow.py'
    source.write_text('value = 1\n')
    module.__file__ = str(source)
    monkeypatch.setattr(ledger, '_module_snapshot', lambda: (('slow', module),))
    entered, release, at_mint = (threading.Event() for _ in range(3))
    real_resolve, real_token = Path.resolve, runtime_bootstrap.secrets.token_urlsafe
    errors, mint_before_release = [], []

    def resolve(path, *args, **kwargs):
        if path == source:
            entered.set()
            if not release.wait(5):
                raise AssertionError('resolver watchdog elapsed')
        return real_resolve(path, *args, **kwargs)

    def token(size):
        # Controlled audit ingress at the real mint boundary; not a claim that
        # secrets always emits this event, or a global installed-hook proof.
        at_mint.set()
        ledger._audit('import', ('ticket_fixture_ingress', None))
        mint_before_release.append(not release.is_set())
        return real_token(size)

    monkeypatch.setattr(Path, 'resolve', resolve)
    monkeypatch.setattr(runtime_bootstrap.secrets, 'token_urlsafe', token)

    def inventory():
        try:
            ledger.integrity_observation()
        except BaseException as exc:
            errors.append(exc)

    def watchdog():
        if not at_mint.wait(3):
            errors.append(AssertionError('native ticket did not reach actual mint'))
        release.wait(0.5)
        release.set()

    async def scenario():
        home = tmp_path / 'home'
        home.mkdir(mode=0o700)
        server = GatewayControlServer(home)
        store = TicketStore('inventory-test-instance', frozenset({'default'}))
        server.ticket_store = store
        assert await server.start()
        reader, unlocker = threading.Thread(target=inventory), threading.Thread(target=watchdog)
        try:
            reader.start()
            assert await asyncio.to_thread(entered.wait, 3)
            unlocker.start()
            response = await asyncio.to_thread(
                query_gateway_control, home, 'session-ticket', timeout=2,
                params={'profile_id': 'default', 'instance_id': store.instance_id, 'purpose': 'native-http'})
            assert response is not None
            grant = store.redeem(response['ticket'], profile_id='default', purpose='native-http')
            assert grant['capabilities'] == frozenset({'http:owner'})
            assert grant['subject'] == f'uid:{os.getuid()}'
        finally:
            release.set()
            if reader.ident is not None:
                await asyncio.to_thread(reader.join, 5)
            if unlocker.ident is not None:
                await asyncio.to_thread(unlocker.join, 5)
            await server.stop()
        assert not reader.is_alive() and not unlocker.is_alive()
        assert errors == []
        assert mint_before_release == [True], 'native grant waited for inventory filesystem work'

    asyncio.run(scenario())
