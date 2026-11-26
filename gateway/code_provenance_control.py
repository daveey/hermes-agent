"""Private, passive, diagnostic-only provenance pages; never authorization.

A timed-out filesystem operation cannot be killed safely. A single daemon worker
retains the global flight lock until the real operation ends. Its late result is
never published. No executor queue, retries, source reads or persistent files.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time

from hermes_cli.code_attestation_snapshot import SECTIONS
from hermes_cli.code_provenance_progress import CaptureProgress, unavailable


CAPTURE_SECONDS = 2.0
TTL_SECONDS = 30.0
MAX_PAGE_BYTES = 256 * 1024
MAX_PAGE_ENTRIES = 128
_FLIGHT = threading.Lock()


def _error(code):
    return {'purpose': 'diagnostic_only', 'complete': False, 'error': code}


def _settle(result, progress):
    try:
        diagnostics = progress.snapshot()
    except Exception:  # health: allow BLE001 -- private recorder failures must not expose exception text or publish partial success
        return {**_error('capture_failed'), 'diagnostics': unavailable()}
    return {**result, 'diagnostics': diagnostics}


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _expected(value):
    return (type(value) is dict and set(value) == {'pid', 'start_time', 'instance_id'}
            and _integer(value['pid'], 1, 2**63 - 1)
            and _integer(value['start_time'], 1, 2**63 - 1)
            and type(value['instance_id']) is str and 0 < len(value['instance_id']) <= 128)


def _validate(params):
    if type(params) is not dict or type(params.get('schema')) is not int or params['schema'] != 1:
        raise ValueError('invalid_schema')
    if params.get('op') == 'capture':
        if (set(params) != {'schema', 'op', 'expected', 'refresh_inventory'}
                or not _expected(params['expected']) or params['refresh_inventory'] is not True):
            raise ValueError('invalid_capture')
    elif params.get('op') == 'page':
        if (set(params) != {'schema', 'op', 'snapshot_id', 'section', 'offset', 'limit'}
                or type(params['snapshot_id']) is not str or len(params['snapshot_id']) != 64
                or any(c not in '0123456789abcdef' for c in params['snapshot_id'])
                or type(params['section']) is not str or params['section'] not in SECTIONS
                or not _integer(params['offset'], 0, 2**63 - 1)
                or not _integer(params['limit'], 1, MAX_PAGE_ENTRIES)):
            raise ValueError('invalid_page')
    else:
        raise ValueError('invalid_operation')


def validate_envelope(request):
    """Bound echoed IDs so the complete page envelope remains below 256 KiB."""
    identity = request.get('id')
    if (type(request.get('protocol')) is not int or
            not (identity is None or _integer(identity, -(2**63), 2**63 - 1) or
                 (type(identity) is str and len(identity) <= 128))):
        raise ValueError('invalid_provenance_envelope')


class CodeProvenanceControl:
    def __init__(self, runner):
        from gateway.status import _get_process_start_time
        self.runner = runner
        self.pid = os.getpid()
        self.start_time = _get_process_start_time(self.pid)
        self._state = threading.Lock()
        self._snapshots = {}

    def identity(self):
        return {'pid': os.getpid(), 'start_time': self.start_time,
                'instance_id': self.runner.session_runtime_descriptor.get('instance_id')}

    def current(self):
        from hermes_cli import code_attestation as ca
        ledger = ca._installed
        # Reading the scalar never waits on an inventory lock or performs inventory.
        return {'identity': self.identity(), 'generation': getattr(ledger, '_generation', None)}

    def handle(self, params, peer):
        if not peer:
            raise PermissionError('authenticated_peer_required')
        _validate(params)
        if not _expected(self.identity()) or os.getpid() != self.pid:
            return _error('process_identity_unavailable')
        if params['op'] == 'capture':
            return self.capture(params, peer)
        return self.page(params, peer)

    def capture(self, params, peer):
        from hermes_cli import code_attestation as ca
        identity = self.identity()
        if params['expected'] != identity:
            return _error('identity_mismatch')
        if not _FLIGHT.acquire(blocking=False):
            return _error('capture_busy')
        deadline = time.monotonic() + CAPTURE_SECONDS
        try:
            progress = CaptureProgress()
        except Exception:  # health: allow BLE001 -- sanitized private startup failure; no exception text
            _FLIGHT.release()
            return {**_error('capture_failed'), 'diagnostics': unavailable()}
        done, box = threading.Event(), {}
        custody = threading.Lock()
        abandoned = False

        def work():
            try:
                progress.set_phase('worker_start')
                ledger = ca._installed
                if not isinstance(ledger, ca.Attestation):
                    box['error'] = 'ledger_unavailable_or_unsupported'
                    return
                if ledger._pid != identity['pid']:
                    box['error'] = 'ledger_process_mismatch'
                    return
                snapshot = ca.diagnostic_snapshot(deadline=deadline, progress=progress)
                disk = dict(snapshot.disk)
                box['value'] = (snapshot, disk, ledger)
            except (OSError, RuntimeError, ValueError, TypeError, AttributeError, TimeoutError) as exc:
                # Fixed/class-only errors, never exception text containing paths/secrets.
                box['error'] = str(exc) if type(exc) is ValueError and str(exc) in {
                    'entry_too_large', 'snapshot_too_large', 'module_metadata_unavailable'} else type(exc).__name__
            except Exception:  # health: allow BLE001 -- settle private worker failures without logging arbitrary secret-bearing exception text
                # Do not expose arbitrary exception messages or custom class names.
                # BaseException remains outside this ordinary-failure contract.
                box['error'] = 'capture_failed'
            finally:
                with custody:
                    done.set()
                    if abandoned:
                        _FLIGHT.release()

        worker = threading.Thread(target=work, name='gateway-code-provenance', daemon=True)
        try:
            worker.start()
        except RuntimeError:
            _FLIGHT.release()
            return _settle(_error('worker_unavailable'), progress)
        completed = done.wait(max(0.0, deadline - time.monotonic()))
        with custody:
            if not completed or time.monotonic() >= deadline:
                abandoned = True
                if done.is_set():
                    _FLIGHT.release()
                return _settle(_error('capture_timeout'), progress)
        try:
            if 'error' in box:
                return _settle(_error(box['error']), progress)
            snapshot, disk, ledger = box['value']
            if self.identity() != identity or ca._installed is not ledger:
                return _settle(_error('identity_changed'), progress)
            # Sample before publication: observer failure cannot replace old pages.
            result = _settle({}, progress)
            if 'error' in result:
                return result
            return {**self._publish(peer, identity, snapshot, disk, ledger), **result}
        finally:
            _FLIGHT.release()

    def _publish(self, peer, identity, snapshot, disk, ledger):
        token = secrets.token_hex(32)
        with self._state:
            now = time.monotonic()
            self._snapshots = {p: s for p, s in self._snapshots.items() if now < s['expires']}
            stored = {'token': token, 'expires': now + TTL_SECONDS,
                'identity': identity, 'snapshot': snapshot, 'disk': disk, 'ledger': ledger}
            self._snapshots[peer] = stored
        return self._summary(stored)

    def _summary(self, stored):
        snapshot = stored['snapshot']
        return {'purpose': 'diagnostic_only', 'complete': True, 'full_provenance': False,
            'snapshot_id': stored['token'], 'ttl_seconds': TTL_SECONDS,
            'captured': {'identity': stored['identity'], 'generation': snapshot.generation,
                         'begin_mono': snapshot.begin_mono, 'end_mono': snapshot.end_mono},
            'current': self.current(), 'disk': stored['disk'], 'ledger': dict(snapshot.boot),
            'sections': {key: dict(value) for key, value in snapshot.manifest.items()},
            'raw': dict(snapshot.manifest['unknowns']),
            'incomplete': ['historical_unretained_provenance'],
            'canonical_json': 'ASCII escaped; sorted keys; compact separators; no NaN; array SHA256'}

    def page(self, params, peer):
        from hermes_cli import code_attestation as ca
        with self._state:
            stored = self._snapshots.get(peer)
            if stored is None or stored['token'] != params['snapshot_id']:
                return _error('snapshot_unavailable')
            if time.monotonic() >= stored['expires']:
                del self._snapshots[peer]
                return _error('snapshot_expired')
            if self.identity() != stored['identity'] or ca._installed is not stored['ledger']:
                del self._snapshots[peer]
                return _error('snapshot_identity_changed')
        rows = stored['snapshot'].sections[params['section']]
        offset = params['offset']
        if offset > len(rows):
            return _error('offset_out_of_range')
        result = self._summary(stored)
        result.update(section=params['section'], offset=offset, entries=[], next=None)
        for index in range(offset, min(len(rows), offset + params['limit'])):
            result['entries'].append(json.loads(rows[index]))
            result['next'] = index + 1 if index + 1 < len(rows) else None
            # Match the actual transport's default json.dumps, including envelope headroom.
            if len(json.dumps(result).encode('utf-8')) + 4096 > MAX_PAGE_BYTES:
                result['entries'].pop()
                result['next'] = index
                break
        if not result['entries'] and offset < len(rows):
            return _error('entry_too_large')
        return result


def register_code_provenance_control(runner, server):
    control = CodeProvenanceControl(runner)
    server.private_handlers['code-provenance'] = control.handle
    return control
