"""Fail-closed admission orchestration; deliberately not wired to serve yet.

The host supplies strict collectors and canonical process classification, never
client-supplied receipts. A COMPLETE cut is scoped to one held gate acquisition;
there is no persisted clean bit that a successor or boot may reuse. This module
provides no default permissive collector/classifier and does not start producers.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from hermes_cli.serve_producer_census import CensusUnavailable, Incarnation, ProcessCensus
from hermes_cli.serve_producer_custody import FamilyCustody, _acquire_runtime

# Each collector must enumerate its exact configured scope (including disabled or
# empty scope receipts), distinguish errors and return positive evidence refs.
REQUIRED_COLLECTORS = frozenset(('raw_ledger', 'profile_controls', 'supervisor_ports',
                                 'supported_launchers', 'installation_access'))


class AdmissionUnavailable(RuntimeError):
    """External production stays dormant while exclusion cannot be established."""


@dataclass(frozen=True)
class CollectorEvidence:
    refs: tuple[str, ...]
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class Classification:
    identity: Incarnation
    kind: str  # participant or positively unrelated; legacy/unknown are conflicts
    proof: str


@dataclass(frozen=True)
class AdmissionCut:
    boot_id: str
    generation: str
    installation: str
    census: ProcessCensus | None
    collectors: tuple[tuple[str, CollectorEvidence], ...]
    classifications: tuple[Classification, ...]
    legacy_candidates: tuple[Incarnation, ...]
    retired: tuple[Incarnation, ...]
    errors: tuple[str, ...]
    _acquisition: object

    @property
    def complete(self) -> bool:
        return not self.errors


class LaunchGate:
    """Stable nonblocking launch mutex; hold through registration+family admission.

    This gate alone does not fence pre-integration processes. Family custody must
    outlive this short launch critical section and all live producer callbacks.
    """

    def __init__(self, fd: int):
        self._custody = FamilyCustody(fd)
        self._pid = os.getpid()
        self._acquisition = object()

    @classmethod
    def acquire(cls, runtime: Path) -> LaunchGate:
        return cls(_acquire_runtime(runtime, 'admission'))

    def __enter__(self):
        self._held()
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        self._custody.close()

    def _held(self):
        if self._custody.fd is None or os.getpid() != self._pid:
            raise AdmissionUnavailable('launch gate must remain held by collecting process')

    def inspect(self, *, boot_id: str, generation: str, installation: str,
                census, collectors: dict, classify, legacy_candidates: tuple[Incarnation, ...],
                exited) -> AdmissionCut:
        """Run strict host observations inside the gate; never from an HTTP path.

        `exited` must affirm the exact candidate incarnation is gone, including
        spawners, not just absent from the current ledger. Exceptions are unknown.
        All adapters are mandatory: no best-effort inventory fallback exists.
        """
        self._held()
        errors, evidence, classified, retired = [], [], [], []
        if not all(isinstance(value, str) and value for value in (boot_id, generation, installation)):
            errors.append('identity:missing')
        for name in sorted(REQUIRED_COLLECTORS):
            _collect(name, collectors.get(name), evidence, errors)
        observation = _census(census, boot_id, errors)
        if observation is not None:
            for record in observation.processes:
                _classify(record, classify, classified, errors)
        for identity in legacy_candidates:
            try:
                if exited(identity) is not True:
                    errors.append(f'legacy:{identity.pid}:not_retired')
                else:
                    retired.append(identity)
            except (OSError, ValueError, CensusUnavailable) as exc:
                errors.append(f'legacy:{identity.pid}:{type(exc).__name__}')
        self._held()
        return AdmissionCut(boot_id, generation, installation, observation, tuple(evidence),
                            tuple(classified), tuple(legacy_candidates), tuple(retired),
                            tuple(errors), self._acquisition)

    def require(self, cut: AdmissionCut, *, boot_id: str, generation: str, installation: str):
        self._held()
        if cut._acquisition is not self._acquisition:
            raise AdmissionUnavailable('different launch-gate acquisition')
        if (cut.boot_id, cut.generation, cut.installation) != (boot_id, generation, installation):
            raise AdmissionUnavailable('admission identity changed')
        if not cut.complete:
            raise AdmissionUnavailable('incomplete producer-admission evidence')


def _collect(name, callback, evidence, errors):
    if callback is None:
        errors.append(f'{name}:missing')
        return
    try:
        result = callback()
        if not isinstance(result, CollectorEvidence) or not result.refs or not all(result.refs):
            errors.append(f'{name}:no_positive_evidence')
            return
        evidence.append((name, result))
        errors.extend(f'{name}:{conflict}' for conflict in result.conflicts)
    except (OSError, ValueError, CensusUnavailable) as exc:
        errors.append(f'{name}:{type(exc).__name__}')


def _census(callback, boot_id, errors):
    try:
        result = callback()
        if not isinstance(result, ProcessCensus):
            errors.append('processes:missing')
            return None
        errors.extend(f'processes:{error}' for error in result.errors)
        if result.boot_id != boot_id:
            errors.append('processes:boot_changed')
        return result
    except (OSError, ValueError, CensusUnavailable) as exc:
        errors.append(f'processes:{type(exc).__name__}')
        return None


def _classify(record, callback, classified, errors):
    try:
        result = callback(record)
        if not isinstance(result, Classification) or result.identity != record.identity:
            errors.append(f'pid:{record.identity.pid}:classification_identity')
            return
        classified.append(result)
        if result.kind not in ('participant', 'unrelated') or not result.proof:
            errors.append(f'pid:{record.identity.pid}:unproven_classification')
    except (OSError, ValueError, CensusUnavailable) as exc:
        errors.append(f'pid:{record.identity.pid}:{type(exc).__name__}')
