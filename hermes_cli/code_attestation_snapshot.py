"""Diagnostic metadata only. Canonical JSON: ASCII escaped, sorted keys, compact, no NaN.

The digest of a section is SHA256 of its canonical JSON array (including []),
not of a transport envelope. Entry bytes are immutable and counted while encoding.
No source/code/locals/config/environment or arbitrary receipt fields are copied.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time
from types import MappingProxyType
from typing import Mapping

from hermes_cli.plugin_review_records import REGISTRY
from hermes_cli.code_provenance_disk import disk_identity

MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_ENTRY_BYTES = 240 * 1024  # room for page/envelope metadata under 256 KiB
SECTIONS = ('unknowns', 'quiet_blocking', 'quiet_extension', 'components',
            'dependencies', 'executed_units', 'producers', 'metadata')
ENCODER = json.JSONEncoder(sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
UNIT_KEYS = ('component', 'source_sha256', 'generation', 'filename', 'generator',
             'producer_sha256', 'producer_site', 'interpreter_sha256', 'trust', 'kind', 'sha256')
DIST_KEYS = ('dist', 'version', 'path', 'root', 'record_sha256', 'installer_sha256', 'binary_sha256')
SEED_KEYS = ('module', 'path', 'git_blob', 'source_sha256', 'kind', 'route', 'head_sha256', 'tail_sha256',
             'script_sha256', 'interpreter_binary_sha256', 'relaunch_source_sha256')


@dataclass(frozen=True)
class DiagnosticSnapshot:
    sections: Mapping[str, tuple[bytes, ...]]
    manifest: Mapping[str, Mapping[str, str | int]]
    generation: int
    begin_mono: float
    end_mono: float
    disk: Mapping[str, str | None]
    boot: Mapping[str, str | None]
    full_provenance: bool = False


def scalars(row, keys):
    """Explicit field and value whitelist; never repr(), default=str or recursive objects."""
    return {key: row[key] for key in keys if key in row and
            (row[key] is None or type(row[key]) in (str, int, bool))}


def _preflight(row, deadline):
    """Bound escaped scalars before JSONEncoder can allocate a whole escaped string."""
    pending, size = [row], 0
    while pending:
        value = pending.pop()
        if type(value) is dict:
            for key, item in value.items():
                pending.extend((key, item))
        elif type(value) is str:
            if len(value) > MAX_ENTRY_BYTES:
                raise ValueError('entry_too_large')
            for offset in range(0, len(value), 1024):
                if time.monotonic() >= deadline:
                    raise TimeoutError('snapshot_deadline')
                size += len(ENCODER.encode(value[offset:offset + 1024])) - 2
                if size > MAX_ENTRY_BYTES:
                    raise ValueError('entry_too_large')
        elif value is not None and type(value) not in (int, bool, float):
            raise ValueError('unsupported_metadata_value')


class _Builder:
    def __init__(self, deadline):
        self.deadline = deadline
        self.total = 4096  # fixed manifest/summary budget, in addition to array separators
        self.rows = {section: [] for section in SECTIONS}

    def add(self, section, row):
        _preflight(row, self.deadline)
        chunks, size = [], 0
        for chunk in ENCODER.iterencode(row):
            if time.monotonic() >= self.deadline:
                raise TimeoutError('snapshot_deadline')
            # iterencode can emit one large scalar; check before retaining encoded bytes.
            size += len(chunk)
            if size > MAX_ENTRY_BYTES:
                raise ValueError('entry_too_large')
            if self.total + size + 1 > MAX_SNAPSHOT_BYTES:
                raise ValueError('snapshot_too_large')
            chunks.append(chunk.encode('ascii'))
        self.total += size + 1
        self.rows[section].append(b''.join(chunks))

    def finish(self, generation, begin, disk, boot):
        end = time.monotonic()
        self.add('metadata', {'kind': 'capture', 'generation': generation,
                             'begin_mono': begin, 'end_mono': end})
        if self.total + len(ENCODER.encode(boot)) > MAX_SNAPSHOT_BYTES:
            raise ValueError('snapshot_too_large')
        sections, manifest = {}, {}
        for name, rows in self.rows.items():
            rows.sort()  # canonical entry bytes, lexicographic; duplicates are retained
            digest = hashlib.sha256(b'[')
            for index, row in enumerate(rows):
                if index:
                    digest.update(b',')
                digest.update(row)
            digest.update(b']')
            sections[name] = tuple(rows)
            manifest[name] = MappingProxyType({'count': len(rows), 'sha256': digest.hexdigest()})
        if time.monotonic() >= self.deadline:
            raise TimeoutError('snapshot_deadline')
        return DiagnosticSnapshot(MappingProxyType(sections), MappingProxyType(manifest),
                                  generation, begin, end, MappingProxyType(disk), MappingProxyType(boot))


def _metadata(ledger, builder):
    boot = {'kind': 'ledger', 'core_root': str(ledger.core_root),
        'boot_sha': ledger.boot_sha, 'route': ledger.route,
        'boot_registry_blob': ledger._blobs.get(REGISTRY),
        'boot_registry_status': 'recorded' if REGISTRY in ledger._blobs else 'unavailable',
        'stub_head_digest': ledger.stub_head_digest}
    builder.add('metadata', boot)
    builder.add('metadata', {'kind': 'interpreter', **scalars(ledger.interpreter_identity,
        ('executable', 'version', 'cache_tag', 'prefix', 'base_prefix', 'binary_sha256'))})
    for seed in ledger._seed:
        builder.add('metadata', {'kind': 'seed', 'identity': scalars(seed, SEED_KEYS)})
    if ledger._launcher is not None:
        builder.add('metadata', {'kind': 'launcher', 'identity': scalars(ledger._launcher, SEED_KEYS)})
    builder.add('metadata', {'kind': 'history', 'status': 'unavailable',
        'reason': 'Only retained ledger metadata; historical evicted/generated output is not reconstructed.'})
    builder.add('metadata', {'kind': 'dependency_index', 'limit_reached': ledger._deps.limit,
        'record_is_not_receipted': True, 'native_transitive_libraries': 'unavailable'})
    return boot


def _roots(ledger, builder):
    for component, (root, kind, digests) in ledger._components.items():
        builder.add('components', {'kind': kind, 'component': component, 'root': str(root)})
        for path, digest in digests.items():
            builder.add('components', {'kind': 'reviewed_file', 'component': component,
                                      'path': path, 'sha256': digest})
    # The core map holds boot Git blob identities, not inferred disk SHA256 values.
    for path, digest in ledger._blobs.items():
        builder.add('components', {'kind': 'boot_file', 'component': 'core',
                                  'path': path, 'git_blob': digest})
    for root in ledger._deps.roots:
        builder.add('dependencies', {'kind': 'registered_root', 'root': str(root)})
    for (root, dist), receipt in ledger._deps.dists.items():
        builder.add('dependencies', {'kind': 'installer_receipt', 'root': str(root),
            'distribution': dist, **scalars(receipt, ('record_sha256', 'installer_sha256', 'used'))})


def _execution(ledger, builder):
    for unit in ledger._units:
        row = scalars(unit, UNIT_KEYS)
        if isinstance(unit.get('producer_distribution'), dict):
            row['producer_distribution'] = scalars(unit['producer_distribution'], DIST_KEYS)
        builder.add('executed_units', row)
    for unit in ledger._dep_units.values():
        row = scalars(unit, UNIT_KEYS)
        if isinstance(unit.get('producer_distribution'), dict):
            row['producer_distribution'] = scalars(unit['producer_distribution'], DIST_KEYS)
        builder.add('executed_units', row)
    for load in ledger._native_loads:
        builder.add('executed_units', {'kind': 'native_load', **scalars(load, ('name', 'caller_component'))})
    for descriptor in ledger._generators.values():
        builder.add('producers', {'kind': 'available', 'name': descriptor.name,
            'source_sha256': descriptor.source_sha256, 'producer_kind': descriptor.kind,
            'distribution': scalars(descriptor.distribution, DIST_KEYS)})
    for reason, receipts in ledger._unknown_sources.items():
        for receipt in receipts:
            builder.add('producers', {'kind': 'unknown_source', 'reason': reason,
                **scalars(receipt, ('filename', 'qualname', 'component', 'source_sha256', 'trust'))})
    for reason, producers in ledger._unknown_detail.items():
        for producer in producers:
            builder.add('producers', {'kind': 'unknown_producer', 'reason': reason, 'producer': producer})


def snapshot(ledger, *, deadline: float, progress=None) -> DiagnosticSnapshot:
    """One locked view. Inventory is monotone; a filesystem syscall cannot be cancelled.

    The caller owns single-flight custody until this function actually returns.
    Unsupported launch routes remain readable (with their original sticky refusals).
    """
    begin = time.monotonic()
    if begin >= deadline:
        raise TimeoutError('snapshot_deadline')
    if progress is not None:
        progress.set_phase('disk')
    disk = disk_identity(ledger.core_root, deadline)
    if progress is not None:
        progress.set_phase('ledger_lock')
    if not ledger._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise TimeoutError('snapshot_lock_timeout')
    try:
        if progress is not None:
            progress.set_phase('inventory')
        ledger._settle(deadline=deadline)
        ledger._inventory_modules(deadline=deadline)
        if time.monotonic() >= deadline:
            raise TimeoutError('snapshot_deadline')
        if getattr(ledger, '_reviewed_plugins', None):
            from hermes_cli.plugin_review_records import verify_registered
            if progress is not None:
                progress.set_phase('registry')
            verify_registered(ledger)
        if progress is not None:
            progress.set_phase('projection')
        builder = _Builder(deadline)
        for reason in ledger._unknown:
            builder.add('unknowns', reason)
            extension = reason in ledger._extension_unknowns and reason not in ledger._blocking_unknowns
            builder.add('quiet_extension' if extension else 'quiet_blocking', reason)
        boot = _metadata(ledger, builder)
        builder.add('metadata', {'kind': 'disk', **disk})
        _roots(ledger, builder)
        _execution(ledger, builder)
        if progress is not None:
            progress.set_phase('finalize')
        result = builder.finish(ledger._generation, begin, disk, boot)
        if progress is not None:
            progress.set_phase('complete')
        return result
    finally:
        ledger._lock.release()
