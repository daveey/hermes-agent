"""Actual bootstrap/dispatch and authenticated Unix control round trips."""
import asyncio
import hashlib
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from gateway import code_provenance_control as pc
from gateway.control_dispatch import verb_is_work_bearing
from gateway.run_bootstrap import _start_gateway_start_control_socket
from hermes_cli import code_attestation as ca
from hermes_cli.gateway_client import private_control_query
from tests.hermes_cli import test_code_attestation_snapshot as fixture_helpers

ledger = fixture_helpers.ledger
pytestmark = pytest.mark.platforms('linux', 'macos')


def request(home, params, verb='code-provenance'):
    if verb == 'code-provenance':
        from hermes_cli.code_provenance_client import query
        try:
            return {'ok': True, 'result': query(home, params)}
        except ValueError as exc:
            return {'ok': False, 'error': str(exc)}
    raw = json.dumps({'protocol': 1, 'verb': verb, 'id': 1, 'params': params}).encode() + b'\n'
    return json.loads(private_control_query(home, raw))


@pytest.fixture
def runtime(ledger, tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    monkeypatch.setenv('HERMES_HOME', str(home))
    from gateway import webapp_listener, quiet_control
    # Unrelated registration is inert; production bootstrap and provenance registration are real.
    monkeypatch.setattr(webapp_listener, 'register_control_verbs', lambda *a, **kw: None)
    monkeypatch.setattr(quiet_control, 'register_quiet_control', lambda *a: None)

    runner = SimpleNamespace(session_runtime_descriptor={'instance_id': 'test-instance',
        'state': 'ready', 'capabilities': []}, session_ticket_store=None)
    yield home, runner, ledger


async def start(runtime):
    _home, runner, _ledger = runtime
    server = await _start_gateway_start_control_socket(runner)
    assert server is not None
    assert 'code-provenance' in server.private_handlers
    control = server.private_handlers['code-provenance'].__self__
    params = {'schema': 1, 'op': 'capture', 'expected': control.identity(), 'refresh_inventory': True}
    return server, control, params


async def ask(runtime, params, verb='code-provenance'):
    return await asyncio.to_thread(request, runtime[0], params, verb)


def page_params(summary, **over):
    return {'schema': 1, 'op': 'page', 'snapshot_id': summary['snapshot_id'],
            'section': 'unknowns', 'offset': 0, 'limit': 128, **over}


def test_actual_bootstrap_authenticated_passive_round_trip(runtime):
    async def scenario():
        server, _control, params = await start(runtime)
        try:
            assert not verb_is_work_bearing('code-provenance')
            assert verb_is_work_bearing('code-provenance-other')
            before = set(runtime[2]._unknown)
            generation = runtime[2]._generation
            first = await ask(runtime, params)
            assert first['ok'], first
            summary = first['result']
            assert summary['purpose'] == 'diagnostic_only' and summary['complete']
            assert summary['raw']['count'] == len(before)
            assert runtime[2]._unknown == before
            assert runtime[2]._generation == generation
            page = (await ask(runtime, page_params(summary)))['result']
            assert page['entries'] == sorted(runtime[2]._unknown)
            assert page['next'] is None
            assert page['current']['generation'] == generation
            assert server.ticket_store is None
        finally:
            await server.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('uid', [None, -1])
def test_transport_rejects_missing_or_foreign_peer(runtime, monkeypatch, uid):
    from hermes_cli import gateway_runtime_discovery
    import socket
    original = gateway_runtime_discovery.socket_peer_uid
    monkeypatch.setattr(gateway_runtime_discovery, 'socket_peer_uid',
                        lambda sock: original(sock) if isinstance(sock, socket.socket) else uid)
    async def scenario():
        server, control, params = await start(runtime)
        try:
            reply = await ask(runtime, params)
            assert not reply['ok']
            assert 'provenance_request_refused' in reply['error']
            assert not control._snapshots
        finally:
            await server.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('update', [{'schema': True}, {'schema': 2}, {'extra': 1},
    {'refresh_inventory': 1}, {'refresh_inventory': False}, {'op': 'unknown'},
    {'expected': {'pid': True, 'start_time': 1, 'instance_id': 'x'}},
    {'expected': {'pid': 1, 'start_time': True, 'instance_id': 'x'}},
    {'expected': {'pid': 1, 'start_time': 1, 'instance_id': ''}},
    {'expected': {'pid': 1, 'start_time': 1, 'instance_id': 'x', 'extra': 1}}])
def test_capture_strict_schema(runtime, update):
    async def scenario():
        server, control, params = await start(runtime)
        try:
            reply = await ask(runtime, {**params, **update})
            assert not reply['ok'], reply
            assert not control._snapshots
        finally:
            await server.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('update', [{'offset': True}, {'offset': -1}, {'offset': 1.5},
    {'limit': True}, {'limit': 0}, {'limit': 129}, {'section': 'secrets'}, {'section': []},
    {'snapshot_id': 'g' * 64}, {'snapshot_id': 'a'}, {'extra': 1}])
def test_page_strict_schema(runtime, update):
    async def scenario():
        server, _control, params = await start(runtime)
        try:
            summary = (await ask(runtime, params))['result']
            reply = await ask(runtime, page_params(summary, **update))
            assert not reply['ok'], reply
        finally:
            await server.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [KeyError, ZeroDivisionError])
def test_unexpected_inventory_failure_is_sanitized_and_recoverable(runtime, monkeypatch, failure):
    hooks, workers = [], []
    monkeypatch.setattr(threading, 'excepthook', hooks.append)
    async def scenario():
        server, control, params = await start(runtime)
        try:
            summary = (await ask(runtime, params))['result']
            original = runtime[2]._inventory_modules
            def broken(**kwargs):
                workers.append(threading.current_thread())
                raise failure('PRIVATE_INVENTORY_EXCEPTION_SENTINEL')
            monkeypatch.setattr(runtime[2], '_inventory_modules', broken)
            raw = json.dumps({'protocol': 1, 'verb': 'code-provenance',
                              'id': 1, 'params': params}).encode() + b'\n'
            reply = json.loads(await asyncio.to_thread(private_control_query, runtime[0], raw))
            assert len(workers) == 1
            await asyncio.to_thread(workers[0].join, 2)
            assert not workers[0].is_alive()
            assert reply == {'protocol': 1, 'id': 1, 'ok': True,
                             'result': pc._error('capture_failed')}
            assert 'snapshot_id' not in reply['result']
            assert not hooks
            assert 'PRIVATE_INVENTORY_EXCEPTION_SENTINEL' not in json.dumps(reply)
            assert next(iter(control._snapshots.values()))['token'] == summary['snapshot_id']
            page = (await ask(runtime, page_params(summary)))['result']
            assert page['complete'] and page['snapshot_id'] == summary['snapshot_id']
            assert not pc._FLIGHT.locked()
            monkeypatch.setattr(runtime[2], '_inventory_modules', original)
            fresh = (await ask(runtime, params))['result']
            assert fresh['complete'] and fresh['snapshot_id'] != summary['snapshot_id']
            assert not hooks
        finally:
            await server.stop()
    asyncio.run(scenario())


def test_late_unexpected_failure_keeps_flight_until_worker_exit(runtime, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    hooks, workers = [], []
    monkeypatch.setattr(threading, 'excepthook', hooks.append)
    async def scenario():
        server, control, params = await start(runtime)
        try:
            summary = (await ask(runtime, params))['result']
            original = runtime[2]._inventory_modules
            def blocked(**kwargs):
                workers.append(threading.current_thread())
                entered.set()
                assert release.wait(8)
                raise KeyError('PRIVATE_LATE_EXCEPTION_SENTINEL')
            monkeypatch.setattr(runtime[2], '_inventory_modules', blocked)
            pending = asyncio.create_task(ask(runtime, params))
            assert await asyncio.to_thread(entered.wait, 2)
            assert (await pending)['result'] == pc._error('capture_timeout')
            assert (await ask(runtime, params))['result'] == pc._error('capture_busy')
            assert (await ask(runtime, page_params(summary)))['result']['complete']
            assert pc._FLIGHT.locked()
            release.set()
            await asyncio.to_thread(workers[0].join, 2)
            assert not workers[0].is_alive() and not hooks
            assert not pc._FLIGHT.locked()
            assert next(iter(control._snapshots.values()))['token'] == summary['snapshot_id']
            monkeypatch.setattr(runtime[2], '_inventory_modules', original)
            assert (await ask(runtime, params))['result']['complete']
        finally:
            release.set()
            for worker in workers:
                await asyncio.to_thread(worker.join, 2)
            await server.stop()
    asyncio.run(scenario())


def test_missing_ledger_is_error_and_never_installs(runtime, monkeypatch):
    monkeypatch.setattr(ca, '_installed', None)
    async def scenario():
        server, _control, params = await start(runtime)
        try:
            result = (await ask(runtime, params))['result']
            assert result == pc._error('ledger_unavailable_or_unsupported')
            assert ca._installed is None
        finally:
            await server.stop()
    asyncio.run(scenario())


def test_pagination_over_transport_limit_frozen_generation_secrets(runtime, monkeypatch):
    a = runtime[2]
    a._unknown = {f'{i:04d}-' + 'x' * 1500 for i in range(500)}
    monkeypatch.setenv('PROVENANCE_TEST_SECRET', 'SECRET_SENTINEL')
    async def scenario():
        server, _control, params = await start(runtime)
        try:
            summary = (await ask(runtime, params))['result']
            assert summary['complete']
            a.mark_unknown('later')
            received, offset, total_bytes = [], 0, 0
            while offset is not None:
                reply = await ask(runtime, page_params(summary, offset=offset))
                assert len(json.dumps(reply).encode()) < pc.MAX_PAGE_BYTES
                assert 'SECRET_SENTINEL' not in json.dumps(reply)
                result = reply['result']
                total_bytes += len(json.dumps(result['entries']).encode())
                received.extend(result['entries'])
                assert result['current']['generation'] > result['captured']['generation']
                offset = result['next']
            assert total_bytes > 512 * 1024
            assert len(received) == len(set(received)) == 500
            assert received == sorted(a._unknown - {'later'})
            digest = hashlib.sha256(json.dumps(received, sort_keys=True, separators=(',', ':'),
                                               ensure_ascii=True, allow_nan=False).encode()).hexdigest()
            assert summary['raw'] == {'count': len(received), 'sha256': digest}
            fresh = (await ask(runtime, params))['result']
            assert fresh['raw']['count'] == 501
            old = (await ask(runtime, page_params(summary)))['result']
            assert old == pc._error('snapshot_unavailable')  # exactly one snapshot per peer
        finally:
            await server.stop()
    asyncio.run(scenario())


def test_peer_expiry_restart_and_offset_refusals(runtime, monkeypatch):
    async def scenario():
        server, control, params = await start(runtime)
        try:
            summary = (await ask(runtime, params))['result']
            assert control.handle(page_params(summary), 'another-peer') == pc._error('snapshot_unavailable')
            assert (await ask(runtime, page_params(summary, offset=4)))['result'] == pc._error('offset_out_of_range')
            params['expected'] = {**params['expected'], 'pid': os.getpid() + 1}
            assert (await ask(runtime, params))['result'] == pc._error('identity_mismatch')
            stored = next(iter(control._snapshots.values()))
            stored['expires'] = time.monotonic() - 1
            assert (await ask(runtime, page_params(summary)))['result'] == pc._error('snapshot_expired')
            params['expected'] = control.identity()
            summary = (await ask(runtime, params))['result']
            runtime[1].session_runtime_descriptor['instance_id'] = 'replacement'
            assert (await ask(runtime, page_params(summary)))['result'] == pc._error('snapshot_identity_changed')
        finally:
            await server.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['inventory', 'disk', 'plugin'])
def test_blocked_inventory_default_deadline_single_flight_and_responsive_pages(runtime, monkeypatch, stage):
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()
    async def scenario():
        server, control, params = await start(runtime)
        try:
            summary = (await ask(runtime, params))['result']
            def blocked(**kwargs):
                with runtime[2]._lock:
                    entered.set()
                    assert release.wait(8)
                    ended.set()
            if stage == 'inventory':
                monkeypatch.setattr(runtime[2], '_inventory_modules', blocked)
            elif stage == 'disk':
                from hermes_cli import code_attestation_snapshot as projection
                def blocked_disk(*args):
                    blocked()
                    return {'sha': 'b' * 40, 'error': None}
                monkeypatch.setattr(projection, 'disk_identity', blocked_disk)
            else:
                from hermes_cli import plugin_review_records
                runtime[2]._reviewed_plugins = {'test': True}
                monkeypatch.setattr(plugin_review_records, 'verify_registered', lambda ledger: blocked())
            pending = asyncio.create_task(ask(runtime, params))
            assert await asyncio.to_thread(entered.wait, 2)
            assert (await ask(runtime, params))['result'] == pc._error('capture_busy')
            page = await asyncio.wait_for(ask(runtime, page_params(summary)), 1)
            assert page['result']['complete']
            identified = await asyncio.wait_for(ask(runtime, {}, 'identify'), 2)
            assert identified['result']['pid'] == os.getpid()
            assert identified['result']['instance_id'] == params['expected']['instance_id']
            reply = await pending  # real client default timeout, production 2s capture budget
            assert reply['result'] == pc._error('capture_timeout')
            for _ in range(10):
                assert (await ask(runtime, params))['result'] == pc._error('capture_busy')
            workers = [t for t in threading.enumerate() if t.name == 'gateway-code-provenance']
            assert len(workers) == 1
            release.set()
            assert await asyncio.to_thread(ended.wait, 2)
            await asyncio.to_thread(workers[0].join, 2)
            assert not workers[0].is_alive()
            assert next(iter(control._snapshots.values()))['token'] == summary['snapshot_id']
            monkeypatch.setattr(runtime[2], '_inventory_modules', lambda **kw: None)
            fresh = (await ask(runtime, params))['result']
            assert fresh['complete'] and fresh['snapshot_id'] != summary['snapshot_id']
        finally:
            release.set()
            await server.stop()
    asyncio.run(scenario())


def test_transport_envelope_id_and_protocol_are_strict(runtime):
    from gateway.control_socket import _query_unix_socket
    async def scenario():
        server, _control, params = await start(runtime)
        try:
            for update in ({'id': True}, {'id': {}}, {'id': 'x' * 129}, {'id': 2**63}, {'protocol': True}):
                envelope = {'protocol': 1, 'id': 1, 'verb': 'code-provenance', 'params': params, **update}
                raw = await asyncio.to_thread(_query_unix_socket, runtime[0], json.dumps(envelope).encode() + b'\n', 5)
                reply = json.loads(raw)
                assert not reply['ok'], reply
            summary = (await ask(runtime, params))['result']
            envelope = {'protocol': 1, 'id': '\U0001f600' * 128, 'verb': 'code-provenance', 'params': page_params(summary)}
            raw = await asyncio.to_thread(_query_unix_socket, runtime[0], json.dumps(envelope).encode() + b'\n', 5)
            assert len(raw) < pc.MAX_PAGE_BYTES
            assert json.loads(raw)['ok']
        finally:
            await server.stop()
    asyncio.run(scenario())


def test_export_has_no_reservations_runner_or_file_mutations(runtime, monkeypatch):
    async def scenario():
        server, _control, params = await start(runtime)
        try:
            def forbidden(*args, **kwargs):
                pytest.fail('passive export attempted a reservation or registration')
            monkeypatch.setattr(server._request_dispatch, '_reserve', forbidden)
            monkeypatch.setattr(ca, 'install', forbidden)
            monkeypatch.setattr(ca, 'register_component_root', forbidden)
            monkeypatch.setattr(ca, 'register_dependency_environment', forbidden)
            from hermes_cli import plugin_review_records
            monkeypatch.setattr(plugin_review_records, 'register_directory', forbidden)
            before_runner = dict(vars(runtime[1]))
            before_files = {str(p): (p.stat().st_size, p.stat().st_mtime_ns)
                            for p in runtime[0].rglob('*')}
            before = (set(runtime[2]._unknown), runtime[2]._generation)
            summary = (await ask(runtime, params))['result']
            for section in summary['sections']:
                assert (await ask(runtime, page_params(summary, section=section)))['result']['complete']
            assert vars(runtime[1]) == before_runner
            assert before_files == {str(p): (p.stat().st_size, p.stat().st_mtime_ns)
                                    for p in runtime[0].rglob('*')}
            assert before == (runtime[2]._unknown, runtime[2]._generation)
        finally:
            await server.stop()
    asyncio.run(scenario())


def test_full_supported_collector_excludes_code_frame_config_environment(runtime, monkeypatch):
    async def scenario():
        server, _control, params = await start(runtime)
        try:
            from hermes_cli.code_provenance_client import collect
            import sys
            local_secret = 'FRAME_SECRET_SENTINEL'
            runtime[2]._units[0].update(code=(lambda: 'CONSTANT_SECRET_SENTINEL').__code__,
                                      frame=sys._getframe())
            monkeypatch.setenv('PROVENANCE_SECRET_TEST', 'ENV_SECRET_SENTINEL')
            (runtime[0] / 'config.yaml').write_text('token: CONFIG_SECRET_SENTINEL\n')
            document = await asyncio.to_thread(collect, runtime[0], params['expected'])
            assert local_secret not in json.dumps(document)
            assert not any(secret in json.dumps(document) for secret in
                           ('CONSTANT_SECRET_SENTINEL', 'ENV_SECRET_SENTINEL', 'CONFIG_SECRET_SENTINEL'))
            assert document['summary']['ledger']['boot_sha'] == runtime[2].boot_sha
            assert document['sections']['unknowns'] == sorted(runtime[2]._unknown)
        finally:
            runtime[2]._units[0].pop('frame', None)
            await server.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['entry', 'total', 'inventory'])
def test_wire_errors_are_explicit_incomplete_no_secret(runtime, monkeypatch, mode):
    if mode == 'entry':
        runtime[2]._unknown = {'x' * 300000}
    elif mode == 'total':
        from hermes_cli import code_attestation_snapshot as projection
        monkeypatch.setattr(projection, 'MAX_SNAPSHOT_BYTES', 100)
    else:
        def fail(**kwargs):
            runtime[2].mark_unknown('monotone-new-refusal')
            raise OSError('SECRET_SENTINEL')
        monkeypatch.setattr(runtime[2], '_inventory_modules', fail)
    async def scenario():
        server, control, params = await start(runtime)
        try:
            result = (await ask(runtime, params))['result']
            assert result['complete'] is False and 'snapshot_id' not in result
            assert result['purpose'] == 'diagnostic_only'
            assert 'SECRET_SENTINEL' not in json.dumps(result)
            assert result['error'] == {'entry': 'entry_too_large', 'total': 'snapshot_too_large', 'inventory': 'OSError'}[mode]
            assert not control._snapshots
        finally:
            await server.stop()
    asyncio.run(scenario())
