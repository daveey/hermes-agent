"""Host enrollment contracts; no external producer activation is performed here."""
import os
import json
import select
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from hermes_cli import serve_producer_admission as admission
from hermes_cli.serve_producer_census import LinuxProcessCensus, CensusUnavailable

pytestmark = pytest.mark.platforms('linux')


def test_enrollment_is_exact_private_and_does_not_claim_guarded_readiness(tmp_path):
    runtime = tmp_path / 'runtime'
    receipt = admission.enroll_launch(runtime, 'serve', installation=Path(__file__).parents[2])
    current = LinuxProcessCensus().read_process(os.getpid(), LinuxProcessCensus().boot_id())
    assert receipt.identity == current.identity
    assert receipt.observation == admission.observation_digest(current)
    assert receipt.guarded is False
    stored = admission.read_participants_strict(runtime)
    assert stored == (receipt,)
    assert (runtime / 'participants.json').stat().st_mode & 0o777 == 0o600
    assert admission.classify_process(current, stored, receipt.generation,
                                      installation=receipt.installation).kind == 'legacy'


def test_missing_corrupt_or_incorrect_registry_never_becomes_empty(tmp_path):
    with pytest.raises(CensusUnavailable):
        admission.read_participants_strict(tmp_path)
    ledger = tmp_path / 'participants.json'
    for value in ('', '{}', '[null]', '[{"pid":42}]'):
        ledger.write_text(value)
        with pytest.raises(CensusUnavailable):
            admission.read_participants_strict(tmp_path)


def test_pid_reuse_exec_and_boot_change_cannot_reuse_participation(tmp_path):
    receipt = admission.enroll_launch(tmp_path / 'runtime', 'gateway', installation=Path(__file__).parents[2])
    adapter = LinuxProcessCensus()
    current = adapter.read_process(os.getpid(), adapter.boot_id())
    for identity in (replace(current.identity, start_ticks=current.identity.start_ticks + 1),
                     replace(current.identity, boot_id='other-boot')):
        changed = replace(current, identity=identity)
        assert admission.classify_process(changed, (receipt,), receipt.generation,
                                          installation=receipt.installation).kind == 'unknown'
    changed = replace(current, argv=('python', '-m', 'unknown'))
    assert admission.classify_process(changed, (receipt,), receipt.generation,
                                      installation=receipt.installation).kind == 'unknown'


def test_unknown_python_is_not_inferred_unrelated_from_foreign_cwd(tmp_path):
    adapter = LinuxProcessCensus()
    current = adapter.read_process(os.getpid(), adapter.boot_id())
    assert admission.classify_process(replace(current, cwd='/other/project'), (), 'generation',
                                      installation='/other/project').kind == 'unknown'


def test_permission_refusal_is_not_absence(tmp_path, monkeypatch):
    admission.enroll_launch(tmp_path / 'runtime', 'serve', installation=Path(__file__).parents[2])
    def denied(*args, **kwargs):
        raise PermissionError('denied')
    monkeypatch.setattr(admission, '_read_bytes', denied)
    with pytest.raises(CensusUnavailable):
        admission.read_participants_strict(tmp_path / 'runtime')


def test_concurrent_real_launches_keep_both_live_incarnations(tmp_path):
    root = Path(__file__).parents[2]
    runtime = tmp_path / 'runtime'
    code = '''
import sys
from pathlib import Path
from hermes_cli.serve_producer_admission import enroll_launch
print('ready', flush=True)
sys.stdin.readline()
enroll_launch(Path(sys.argv[1]), 'serve', installation=Path.cwd())
print('enrolled', flush=True)
sys.stdin.readline()
'''
    children = []
    try:
        for _ in range(2):
            children.append(subprocess.Popen([sys.executable, '-c', code, str(runtime)], cwd=root,
                                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE, text=True))
        for child in children:
            assert select.select([child.stdout], [], [], 30)[0], 'child startup timeout'
            assert child.stdout.readline().strip() == 'ready'
        for child in children:
            child.stdin.write('start\n')
            child.stdin.flush()
        for child in children:
            assert select.select([child.stdout], [], [], 30)[0], 'enrollment timeout'
            assert child.stdout.readline().strip() == 'enrolled', child.stderr.read()
        receipts = admission.read_participants_strict(runtime)
        assert {row.identity.pid for row in receipts} == {child.pid for child in children}
        assert all(not row.guarded for row in receipts)
    finally:
        for child in children:
            child.communicate(input='exit\n', timeout=30)
            assert child.returncode == 0


def test_retirement_requires_exact_incarnation_and_same_boot():
    adapter = LinuxProcessCensus()
    current = adapter.identity(os.getpid(), adapter.boot_id())
    assert admission.incarnation_exited(adapter, current) is False
    assert admission.incarnation_exited(adapter, replace(current, start_ticks=current.start_ticks + 1)) is True
    with pytest.raises(CensusUnavailable, match='boot'):
        admission.incarnation_exited(adapter, replace(current, boot_id='other-boot'))


@pytest.mark.parametrize('field,value', [('guarded', True), ('guarded', 0), ('role', []),
                                        ('namespace', 'pid:[wrong]')])
def test_forged_readiness_and_malformed_fields_refused(tmp_path, field, value):
    runtime = tmp_path / 'runtime'
    receipt = admission.enroll_launch(runtime, 'serve', installation=Path(__file__).parents[2])
    raw = asdict(receipt)
    raw[field] = value
    (runtime / 'participants.json').write_text(json.dumps([raw]))
    with pytest.raises(CensusUnavailable):
        admission.read_participants_strict(runtime)


def test_foreign_namespace_survives_enrollment_but_refuses_census(tmp_path):
    runtime = tmp_path / 'runtime'
    root = Path(__file__).parents[2]
    receipt = admission.enroll_launch(runtime, 'serve', installation=root)
    foreign = replace(receipt, identity=replace(receipt.identity, pid=receipt.identity.pid + 1_000_000),
                      namespace='pid:[1]')
    (runtime / 'participants.json').write_text(json.dumps([asdict(foreign)]))
    fresh = admission.enroll_launch(runtime, 'serve', installation=root)  # ordinary startup keeps working
    with pytest.raises(CensusUnavailable, match='namespace'):  # but never proves an empty/safe host
        admission.read_participants_strict(runtime)
    rows = admission.read_participants_strict(runtime, allow_foreign_namespace=True)
    assert foreign in rows and fresh in rows
    again = admission.enroll_launch(runtime, 'serve', installation=root)  # replacement namespace row kept
    assert foreign in admission.read_participants_strict(runtime, allow_foreign_namespace=True) and again.namespace != 'pid:[1]'


def test_explicit_hermes_module_is_never_positive_unrelated():
    from hermes_cli.serve_producer_census import Incarnation, ProcessObservation
    record = ProcessObservation(Incarnation('boot', 123, 456), '/opt/other-venv/bin/python', '/tmp',
                                ('/opt/other-venv/bin/python', '-m', 'hermes_cli.main', 'serve'), ())
    verdict = admission.classify_process(record, (), 'generation',
                                         installation='/opt/current-hermes', home='/home/test/.hermes')
    assert verdict.kind == 'legacy'


def test_new_boot_prunes_old_receipts_but_stale_pending_cannot_brick_reused_pid(tmp_path):
    runtime = tmp_path / 'runtime'
    root = Path(__file__).parents[2]
    receipt = admission.enroll_launch(runtime, 'serve', installation=root)
    old = replace(receipt, identity=replace(receipt.identity, boot_id='previous-boot'))
    (runtime / 'participants.json').write_text(json.dumps([asdict(old)]))
    (runtime / f'participants.{os.getpid()}.pending').write_text('interrupted old writer')
    fresh = admission.enroll_launch(runtime, 'serve', installation=root)
    assert admission.read_participants_strict(runtime) == (fresh,)


def test_identical_generation_from_another_install_is_not_participation(tmp_path):
    receipt = admission.enroll_launch(tmp_path / 'runtime', 'serve', installation=Path(__file__).parents[2])
    adapter = LinuxProcessCensus()
    record = adapter.read_process(os.getpid(), adapter.boot_id())
    assert admission.classify_process(record, (receipt,), receipt.generation,
                                      installation='/other-install').kind == 'unrelated'


def test_enrollment_of_a_process_that_does_not_exist_fails_closed_and_publishes_nothing(tmp_path, monkeypatch):
    """Positive admission stays strict: a fake pid (as some fixtures install) is refused, never enrolled.

    Fixtures must scope fake pids to their own module (see tests/gateway/test_agent_cli_visibility.py)
    instead of letting import-time launch enrollment observe them.
    """
    runtime = tmp_path / 'runtime'
    monkeypatch.setattr(os, 'getpid', lambda: 2 ** 22 + 12345)
    with pytest.raises((CensusUnavailable, OSError)):
        admission.enroll_launch(runtime, 'serve', installation=Path(__file__).parents[2])
    monkeypatch.undo()
    assert not (runtime / 'participants.json').exists()
