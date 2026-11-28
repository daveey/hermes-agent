"""Authenticated callback registry snapshots; never discover plugins or invoke a callback.

The serving process captures its actual registrations, not a guess from enabled plugin names. The exec
child must reproduce that exact snapshot before READY. Paths are canonical source identities, not import
instructions: this module deliberately cannot load/register a plugin. Registration-only bootstrap remains
owned by each safe provider. No request or browser field is accepted as registry authority.
"""
from __future__ import annotations

import hashlib
import marshal
from pathlib import Path
import sys
import types

from hermes_cli.serve_producer_protocol import ProtocolError

MAX_CALLBACKS = 64
MAX_SOURCE = 4 * 1024 * 1024
MAX_WRAPPERS = 8
_UNSET = object()


def _source(path):
    try:
        path = Path(path).resolve(strict=True)
        with path.open('rb') as stream:
            data = stream.read(MAX_SOURCE + 1)
    except (OSError, TypeError, ValueError):
        raise ProtocolError('callback source unavailable') from None
    if len(data) > MAX_SOURCE:
        raise ProtocolError('callback source too large')
    return str(path), hashlib.sha256(data).hexdigest()


def _canonical_code(code):
    """Executable identity is path-alias independent, including nested code constants."""
    constants = tuple(_canonical_code(item) if isinstance(item, types.CodeType) else item for item in code.co_consts)
    return code.replace(co_filename=str(Path(code.co_filename).resolve()), co_consts=constants)


def _function(fn):
    if fn is None:
        return None
    chain, seen = [], set()
    while True:
        if not isinstance(fn, types.FunctionType) or id(fn) in seen or len(chain) >= MAX_WRAPPERS:
            raise ProtocolError('callback is not an importable function')
        seen.add(id(fn))
        path, digest = _source(fn.__code__.co_filename)
        # The actual executable code matters: hashing disk alone accepts a monkeypatched __code__.
        # v2 omits object-reference memoization: otherwise code sharing/interning during a pyc load
        # changes the hash across clean interpreters despite identical executable code.
        try:
            defaults = marshal.dumps((fn.__defaults__, fn.__kwdefaults__), 2)
        except ValueError:
            raise ProtocolError('callback defaults are not portable') from None
        chain.append({'path': path, 'sha256': digest, 'qualname': fn.__qualname__,
                      'code': hashlib.sha256(marshal.dumps(_canonical_code(fn.__code__), 2)).hexdigest(),
                      'defaults': hashlib.sha256(defaults).hexdigest()})
        wrapped = getattr(fn, '__wrapped__', None)
        if wrapped is None:
            # Arbitrary closure state cannot be reproduced from a file identity. The supported decorator
            # chain carries its original function explicitly via __wrapped__ (e.g. contextmanager).
            if fn.__closure__:
                raise ProtocolError('callback closure is not portable')
            break
        closure = tuple(cell.cell_contents for cell in (fn.__closure__ or ()))
        if closure != (wrapped,):
            raise ProtocolError('callback wrapper closure is not portable')
        fn = wrapped
    return {**chain[-1], 'wrappers': chain[:-1]}


def _mapping(owner, attr):
    value = getattr(owner, attr, None)
    if type(value) is not dict or len(value) > MAX_CALLBACKS:
        raise ProtocolError('callback registry shape')
    if any(type(name) is not str or not name or len(name) > 256 for name in value):
        raise ProtocolError('callback registry name')
    return dict(value)


def _companion(registry, name, fn, size):
    entry = registry.get(name)
    if entry is None:
        return (None,) * (size - 1)
    if type(entry) is not tuple or len(entry) != size or entry[0] is not fn:
        raise ProtocolError('callback companion identity mismatch')
    return entry[1:]


def capture(*, activity=_UNSET, host=_UNSET):
    """Copy current registry identities without importing core/plugin registrars or calling source functions."""
    if activity is _UNSET:
        activity = sys.modules.get('hermes_cli.kanban_session_activity')
    if host is _UNSET:
        host = sys.modules.get('hermes_cli.kanban_annotation_host')
    if activity is None and host is None:
        return {'done': {}, 'waiting': {}}
    done, waiting = _mapping(activity, '_done_checks'), _mapping(activity, '_waiting_sources')
    declared = _mapping(host, '_declared')
    events, done_events = _mapping(host, '_event_sources'), _mapping(host, '_done_event_sources')
    if set(declared) - set(waiting) or set(events) - set(waiting) or set(done_events) - set(done):
        raise ProtocolError('orphan callback companion')
    result = {'done': {}, 'waiting': {}}
    for kind, functions, declarations in (('done', done, done_events), ('waiting', waiting, events)):
        for name, fn in sorted(functions.items()):
            if fn is None:
                raise ProtocolError('registered callback is missing')
            watch, deadline = _companion(declarations, name, fn, 3)
            inputs = _companion(declared, name, fn, 2)[0] if kind == 'waiting' else None
            result[kind][name] = {'function': _function(fn), 'inputs': _function(inputs),
                                  'watch_paths': _function(watch), 'next_deadline': _function(deadline),
                                  'attention_pass': _function(getattr(fn, 'attention_pass', None))}
    return result


def verify_sources(expected):
    """Authenticate every inventoried source before a registrar can import anything.

    These paths identify files only: this function grants no import/registration permission. A safe provider
    still needs its own bounded root allowlist and registration-only entry points.
    """
    if type(expected) is not dict or set(expected) != {'done', 'waiting'}:
        raise ProtocolError('callback inventory shape')
    fields = {'function', 'inputs', 'watch_paths', 'next_deadline', 'attention_pass'}
    descriptor_fields = {'path', 'sha256', 'qualname', 'code', 'defaults'}
    for kind in ('done', 'waiting'):
        rows = expected[kind]
        if type(rows) is not dict or len(rows) > MAX_CALLBACKS:
            raise ProtocolError('callback inventory rows')
        for name, row in rows.items():
            if type(name) is not str or not name or len(name) > 256:
                raise ProtocolError('callback inventory name')
            if type(row) is not dict or set(row) != fields or row['function'] is None:
                raise ProtocolError('callback inventory entry')
            for descriptor in row.values():
                if descriptor is None:
                    continue
                if type(descriptor) is not dict or set(descriptor) != descriptor_fields | {'wrappers'}:
                    raise ProtocolError('callback descriptor shape')
                wrappers = descriptor['wrappers']
                if type(wrappers) is not list or len(wrappers) >= MAX_WRAPPERS:
                    raise ProtocolError('callback wrapper inventory')
                for item in [{key: value for key, value in descriptor.items() if key != 'wrappers'}, *wrappers]:
                    if type(item) is not dict or set(item) != descriptor_fields:
                        raise ProtocolError('callback descriptor entry')
                    if type(item['qualname']) is not str or not item['qualname']:
                        raise ProtocolError('callback qualname')
                    for key in ('sha256', 'code', 'defaults'):
                        value = item[key]
                        if type(value) is not str or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                            raise ProtocolError('callback digest shape')
                    path, digest = _source(item['path'])
                    if path != item['path'] or digest != item['sha256']:
                        raise ProtocolError('callback source digest mismatch')


def verify(expected, *, activity=_UNSET, host=_UNSET):
    """Fail closed for absent, extra, replaced or disconnected callbacks/companions, and changed loaded bytes."""
    verify_sources(expected)
    if expected != capture(activity=activity, host=host):
        raise ProtocolError('callback inventory mismatch')
