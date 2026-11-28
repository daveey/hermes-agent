"""default_admit's per-participant routing, one assertion per branch (collaborators are fakes)."""
import sys
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from hermes_cli import serve_producer_admission as admission
from hermes_cli import serve_producer_enrollment as enrollment
from hermes_cli import serve_producer_observation as observation
from hermes_cli import serve_producer_participation as participation
from hermes_cli import serve_producer_retirement as retirement
from hermes_cli.serve_producer_lifecycle import default_admit

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='linux census adapter')


@dataclass(frozen=True)
class Cut:
    errors: tuple = ()


class Gate:
    def __init__(self):
        self.kw = None
        self.closed = False

    def inspect(self, **kw):
        self.kw = kw
        return Cut()

    def close(self):
        self.closed = True


class World:
    """Fake hosts: tickets are SimpleNamespaces; behaviour tables decide each collaborator's answer."""

    def __init__(self, tmp_path, monkeypatch):
        self.runtime = tmp_path / 'runtime'
        self.runtime.mkdir()
        (self.runtime / 'participants.json').write_text('')
        install = tmp_path / 'install'
        install.mkdir()
        self.installation = install.resolve()
        self.root = tmp_path / 'root'
        self.gate = Gate()
        self.tickets = []
        self.exited, self.nonhosting, self.shared, self.retired = set(), set(), (), set()
        self.refuse = {}
        self.kinds = {}
        self.retire_calls = []
        monkeypatch.setattr(admission, 'LaunchGate', SimpleNamespace(acquire=lambda runtime: self.gate))
        monkeypatch.setattr(admission, 'LinuxProcessCensus', lambda: SimpleNamespace(
            boot_id=lambda: 'boot', collect=lambda *a: None))
        monkeypatch.setattr(admission, 'host_collectors', lambda root, installation: {})
        monkeypatch.setattr(admission, 'incarnation_exited', lambda adapter, key: key in self.exited)
        monkeypatch.setattr(admission, 'launcher_generation', lambda installation: 'G')
        monkeypatch.setattr(admission, 'read_participants_strict', lambda runtime: tuple(self.tickets))
        monkeypatch.setattr(enrollment, 'classify_with_broker', lambda record, participants, generation, **kw:
                            admission.Classification(record.key, self.kinds.get(record.key, 'unrelated'), 'baseline'))
        monkeypatch.setattr(retirement, 'request_retirement', self.request_retirement)
        monkeypatch.setattr(participation, 'nonhosting_participant',
                            lambda key, participants, generation, installation: key in self.nonhosting)
        monkeypatch.setattr(participation, 'retired_participant',
                            lambda record, participants, runtime, generation, installation: record.key in self.retired)
        monkeypatch.setattr(observation, 'observe_shared_family',
                            lambda runtime, installation, participants, generation, gate: self.shared)

    def request_retirement(self, runtime, ticket, root, gate):
        self.retire_calls.append(ticket.key)
        if ticket.key in self.refuse:
            raise self.refuse[ticket.key]

    def ticket(self, key, *, generation='G', installation=None, role='serve', pid=100):
        row = SimpleNamespace(key=key, generation=generation, role=role, identity=SimpleNamespace(pid=pid),
                              installation=str(self.installation) if installation is None else installation)
        self.tickets.append(row)
        return row

    def admit(self):
        return default_admit(self.runtime, self.installation, root=self.root)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def test_participants_are_routed_to_legacy_candidates_or_retirement_requests(world):
    world.ticket('gone')
    world.ticket('old-generation', generation='OLD')
    world.ticket('other-install', installation='/elsewhere')
    world.ticket('good')
    world.exited = {'gone'}
    gate, cut = world.admit()
    assert gate is world.gate and cut.errors == ()
    assert world.gate.kw['legacy_candidates'] == ('gone', 'old-generation', 'other-install')
    assert world.retire_calls == ['good']
    assert world.gate.kw['generation'] == 'G' and world.gate.kw['installation'] == str(world.installation)
    assert world.gate.kw['exited']('gone') is True and world.gate.kw['exited']('good') is False


def test_external_brokers_and_nonhosting_roles_are_not_asked_to_retire(world):
    world.ticket('broker', role='producer_broker')
    world.ticket('gateway')
    world.ticket('good')
    world.nonhosting = {'gateway'}
    world.admit()
    assert world.retire_calls == ['good']
    assert world.gate.kw['legacy_candidates'] == ()


def test_retirement_refusals_are_recorded_and_unexpected_failures_close_the_gate(world):
    world.ticket('a', pid=11)
    world.ticket('b', pid=12)
    world.ticket('c', pid=13)
    world.refuse = {'a': OSError('x'), 'b': ValueError('x'), 'c': admission.AdmissionUnavailable('x')}
    _, cut = world.admit()
    assert cut.errors == ('retirement:11:OSError', 'retirement:12:ValueError',
                          'retirement:13:AdmissionUnavailable')
    assert not world.gate.closed
    world.refuse = {'a': RuntimeError('boom')}
    with pytest.raises(RuntimeError):
        world.admit()
    assert world.gate.closed


def test_classification_precedence_and_exact_predicates(world):
    keys = ['shared', 'nonhosting', 'retired', 'retired-not-legacy', 'legacy-unretired']
    records = {key: SimpleNamespace(key=key) for key in keys}
    world.shared = (records['shared'],)
    world.nonhosting = {'nonhosting'}
    world.retired = {'retired', 'retired-not-legacy'}
    world.kinds = {'retired': 'legacy', 'retired-not-legacy': 'unrelated', 'legacy-unretired': 'legacy',
                   'shared': 'legacy', 'nonhosting': 'legacy'}
    world.admit()
    classify = world.gate.kw['classify']
    got = {key: classify(record) for key, record in records.items()}
    assert (got['shared'].kind, got['shared'].proof) == ('participant', 'panel-shared-kernel:G')
    assert (got['nonhosting'].kind, got['nonhosting'].proof) == ('participant', 'panel-nonhosting-role:G')
    assert (got['retired'].kind, got['retired'].proof) == ('participant', 'panel-retired-kernel:G')
    assert (got['retired-not-legacy'].kind, got['retired-not-legacy'].proof) == ('unrelated', 'baseline')
    assert (got['legacy-unretired'].kind, got['legacy-unretired'].proof) == ('legacy', 'baseline')
