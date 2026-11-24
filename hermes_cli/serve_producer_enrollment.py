"""Process-local broker custody evidence, never a persisted guarded boolean.

Only the exec broker may promote its own enrollment ticket. This narrow adapter
certifies the broker, not arbitrary serves or its children. Legacy serves remain
conflicts and production startup is still unavailable in this preparatory slice.
"""
from __future__ import annotations

import os
from pathlib import Path

from hermes_cli.serve_producer_admission import (
    AdmissionUnavailable, LaunchGate, classify_process, enroll_launch,
    launcher_generation, observation_digest, read_participants_strict,
)
from hermes_cli.serve_producer_census import LinuxProcessCensus
from hermes_cli.serve_producer_custody import FamilyCustody


class BrokerEnrollment:
    """Own actual panel custody before any provider import, under the launch gate.

    No serialized form is accepted. The descriptor and exact self observation
    must remain live on every classification. A successor must collect again.
    """

    def __init__(self, runtime, installation, ticket, owner):
        self.runtime = runtime
        self.installation = installation
        self.ticket = ticket
        self.owner = owner
        self._inode = os.fstat(owner.fd)

    @classmethod
    def acquire(cls, runtime: Path, installation: Path):
        runtime = runtime.resolve()
        installation = installation.resolve(strict=True)
        ticket = enroll_launch(runtime, 'producer_broker', installation=installation)
        with LaunchGate.acquire(runtime):
            adapter = LinuxProcessCensus()
            record = adapter.read_process(os.getpid(), adapter.boot_id())
            expected = str(installation / 'hermes_cli/serve_producer_process.py')
            if len(record.argv) < 5 or record.argv[1:4] != ('-I', expected, 'broker'):
                raise AdmissionUnavailable('only exec broker can claim custody enrollment')
            if (ticket not in read_participants_strict(runtime)
                    or ticket.identity != record.identity
                    or ticket.observation != observation_digest(record)):
                raise AdmissionUnavailable('broker enrollment changed')
            owner = FamilyCustody.acquire(runtime, 'panel')
            try:
                return cls(runtime, installation, ticket, owner)
            except BaseException:
                owner.close()
                raise

    def matches(self, record, *, runtime, installation, generation):
        """Read local custody, never trust ticket.guarded or a caller readiness bit."""
        if (self.owner.fd is None or record.identity.pid != os.getpid()
                or Path(runtime).resolve() != self.runtime
                or str(self.installation) != installation
                or self.ticket.identity != record.identity
                or self.ticket.observation != observation_digest(record)
                or self.ticket.generation != generation
                or launcher_generation(self.installation) != generation):
            return False
        current = os.fstat(self.owner.fd)
        path = (self.runtime / 'panel.lock').stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (self._inode.st_dev, self._inode.st_ino):
            return False
        if (path.st_dev, path.st_ino) != (current.st_dev, current.st_ino):
            return False
        # fstat alone would accept a closed descriptor reused for the same inode.
        # Linux fdinfo is the current kernel observation for this exact descriptor.
        info = Path(f'/proc/self/fdinfo/{self.owner.fd}').read_text(encoding='ascii')
        return any(line.startswith('lock:') and ' FLOCK ' in line
                   and ' WRITE ' in line and f' {os.getpid()} ' in line
                   for line in info.splitlines())

    def close(self):
        self.owner.close()


def classify_with_broker(record, participants, generation, *, installation, runtime, broker=None, home=''):
    """Only a live local BrokerEnrollment can promote its exact enrolled record."""
    from hermes_cli.serve_producer_admission import Classification

    baseline = classify_process(record, participants, generation, installation=installation, home=home)
    if (baseline.kind == 'legacy' and type(broker) is BrokerEnrollment
            and broker.ticket in participants
            and broker.matches(record, runtime=runtime, installation=installation, generation=generation)):
        return Classification(record.identity, 'participant', f'panel-broker-custody:{generation}')
    return baseline
