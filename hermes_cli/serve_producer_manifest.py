"""Exact producer-safe provider manifest (dormant).

Declared, then verified twice: BEFORE any provider import (bytes on disk match their digests, every
required section is present) and AFTER the import but before READY (everything the import actually
loaded from the provider directory is declared with a matching digest). A missing section is not
"disabled": it refuses, so an absent provider/callback/mode can never become fresh-empty success.

Shape (version 1)::

    {"version": 1,
     "providers": {name: {"path": <relative>, "sha256": <hex>}},      # enabled safe providers, >= 1
     "callbacks": {"done": [{"name", "path", "sha256"}], "waiting": [...]},   # registration-only; keys required
     "modes": {source: mode},                                          # effective source modes
     "facts": {switch: value}}                                         # effective FACTS switches
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from hermes_cli.serve_producer_protocol import ProtocolError

VERSION = 1
MAX_ENTRIES = 64
MAX_FILE = 4 * 1024 * 1024
_SCALARS = (str, bool, int)


def _digest(value) -> bool:
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _relative(value) -> Path:
    if type(value) is not str or not value:
        raise ProtocolError('manifest path')
    member = Path(value)
    if member.is_absolute() or '..' in member.parts:
        raise ProtocolError('manifest path')
    return member


def _settings(section, name) -> None:
    if type(section) is not dict or len(section) > MAX_ENTRIES:
        raise ProtocolError(f'manifest {name} shape')
    for key, value in section.items():
        if type(key) is not str or not key or type(value) not in _SCALARS:
            raise ProtocolError(f'manifest {name} entry')


def declared_files(manifest) -> dict:
    """Relative path -> digest for every file the manifest declares (validates the whole shape)."""
    if type(manifest) is not dict or type(manifest.get('version')) is not int or manifest['version'] != VERSION:
        raise ProtocolError('manifest version')
    providers, callbacks = manifest.get('providers'), manifest.get('callbacks')
    if type(providers) is not dict or not providers or len(providers) > MAX_ENTRIES:
        raise ProtocolError('manifest providers')
    if type(callbacks) is not dict or set(callbacks) != {'done', 'waiting'}:
        raise ProtocolError('manifest callbacks')
    _settings(manifest.get('modes'), 'modes')
    _settings(manifest.get('facts'), 'facts')
    files = {}

    def add(entry, label):
        if type(entry) is not dict or not _digest(entry.get('sha256')):
            raise ProtocolError(f'manifest {label} entry')
        path = _relative(entry.get('path')).as_posix()
        if files.setdefault(path, entry['sha256']) != entry['sha256']:
            raise ProtocolError(f'manifest {label} conflicting digest')

    for name, entry in providers.items():
        if type(name) is not str or not name:
            raise ProtocolError('manifest providers entry')
        add(entry, 'provider')
    for kind in ('done', 'waiting'):
        rows = callbacks[kind]
        if type(rows) is not list or len(rows) > MAX_ENTRIES:
            raise ProtocolError('manifest callbacks shape')
        for entry in rows:
            if type(entry) is not dict or type(entry.get('name')) is not str or not entry['name']:
                raise ProtocolError('manifest callbacks entry')
            add(entry, 'callback')
    return files


def _read(path: Path) -> bytes:
    try:
        data = path.read_bytes()
    except OSError:
        raise ProtocolError('manifest member missing') from None
    if len(data) > MAX_FILE:
        raise ProtocolError('manifest member too large')
    return data


def verify_declared(manifest, base: Path) -> dict:
    """Pre-import: every declared file exists under ``base`` with exactly its digest."""
    files = declared_files(manifest)
    if 'callback_registry' in manifest:
        from hermes_cli.serve_producer_callbacks import verify_sources
        verify_sources(manifest['callback_registry'])
    for relative, digest in files.items():
        if hashlib.sha256(_read(base / relative)).hexdigest() != digest:
            raise ProtocolError('manifest digest mismatch')
    return files


def verify_loaded(manifest, base: Path, *, allowed=(), modules=None) -> None:
    """Post-import: nothing was loaded from ``base`` that the manifest (or ``allowed``) does not name."""
    declared = dict(declared_files(manifest))
    if 'callback_registry' in manifest:
        from hermes_cli.serve_producer_callbacks import verify
        verify(manifest['callback_registry'])
    for name in allowed:
        declared.setdefault(_relative(name).as_posix(), None)
    root = base.resolve()
    for module in list((sys.modules if modules is None else modules).values()):
        origin = getattr(module, '__file__', None)
        if not origin:
            continue
        try:
            relative = Path(origin).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            continue
        digest = declared.get(relative, False)
        if digest is False:
            raise ProtocolError('undeclared provider module loaded')
        if digest is not None and hashlib.sha256(_read(root / relative)).hexdigest() != digest:
            raise ProtocolError('loaded provider module digest mismatch')
