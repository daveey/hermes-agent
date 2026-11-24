"""Review-driven boundaries of admission classification and raw-file observation.

* A multi-entry/relative/indirect PYTHONPATH that reaches the installation is never
  "unrelated" (that would exclude a live legacy importer from the census).
* A non-regular file (FIFO, device, symlink, or one swapped in after a path check) in
  a ledger slot is uncertainty, and never holds the launch gate.
"""
import os
import select
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from hermes_cli import serve_producer_admission as admission
from hermes_cli import serve_producer_census as census
from hermes_cli.serve_producer_census import (
    CensusUnavailable, Incarnation, LinuxProcessCensus, ProcessObservation,
)
from hermes_cli.serve_producer_custody import CustodyConflict

pytestmark = pytest.mark.platforms('linux')

ROOT = Path(__file__).parents[2]


def _record(*, cwd='/foreign', argv=('python', 'tool.py'), env=(), exe='/usr/bin/python'):
    return ProcessObservation(Incarnation('boot', 321, 9), exe, cwd, tuple(argv), tuple(sorted(env)))


def _kind(record, installation, home=''):
    return admission.classify_process(record, (), 'generation', installation=str(installation),
                                      home=str(home)).kind


# ---- PYTHONPATH / path-reference classification --------------------------------------

@pytest.mark.parametrize('layout', ['first', 'middle', 'last'])
def test_multi_entry_pythonpath_containing_installation_is_never_unrelated(tmp_path, layout):
    install = tmp_path / 'install'
    install.mkdir()
    entries = {'first': [install, tmp_path / 'a'], 'middle': [tmp_path / 'a', install, tmp_path / 'b'],
               'last': [tmp_path / 'a', tmp_path / 'b', install]}[layout]
    record = _record(env=(('PYTHONPATH', os.pathsep.join(map(str, entries))),))
    assert _kind(record, install) == 'unknown'


def test_pythonpath_entry_inside_installation_subtree_is_never_unrelated(tmp_path):
    install = tmp_path / 'install'
    (install / 'pkg').mkdir(parents=True)
    record = _record(env=(('PYTHONPATH', f'{tmp_path}/a{os.pathsep}{install}/pkg'),))
    assert _kind(record, install) == 'unknown'


def test_unrelated_multi_entry_pythonpath_remains_positively_unrelated(tmp_path):
    install = tmp_path / 'install'
    install.mkdir()
    sibling = tmp_path / 'install-sibling'  # component boundary, not a string prefix
    record = _record(env=(('PYTHONPATH', os.pathsep.join((str(sibling), '/usr/lib/x', '', ''))),),
                     argv=('python', 'tool.py', '--port', '8080'))
    assert _kind(record, install) == 'unrelated'


def test_relative_pythonpath_entry_is_resolved_against_the_observed_cwd(tmp_path):
    install = tmp_path / 'work' / 'install'
    install.mkdir(parents=True)
    # cwd is outside (above) the installation; each relative spelling reaches it.
    for entry in ('install', './install', 'install/', f'x{os.pathsep}install', '../work/install'):
        record = _record(cwd=str(tmp_path / 'work'), env=(('PYTHONPATH', entry),))
        assert _kind(record, install) == 'unknown', entry
    # Controls: relative entries that do NOT reach it.
    for entry in ('other', '.', '../elsewhere'):
        record = _record(cwd=str(tmp_path / 'work'), env=(('PYTHONPATH', entry),))
        assert _kind(record, install) == 'unrelated', entry


def test_dotdot_and_double_slash_spellings_do_not_hide_the_installation(tmp_path):
    install = tmp_path / 'install'
    install.mkdir()
    for spelling in (f'{tmp_path}/other/../install', f'/{tmp_path}/./install', f'{install}//'):
        record = _record(env=(('PYTHONPATH', f'/usr/lib{os.pathsep}{spelling}'),))
        assert _kind(record, install) == 'unknown', spelling


def test_symlink_indirection_in_an_import_search_path_is_never_unrelated(tmp_path):
    install = tmp_path / 'install'
    install.mkdir()
    link = tmp_path / 'alias'
    link.symlink_to(install)
    assert _kind(_record(env=(('PYTHONPATH', f'/usr/lib{os.pathsep}{link}'),)), install) == 'unknown'
    # Relative symlink entry resolved at the observed cwd.
    assert _kind(_record(cwd=str(tmp_path), env=(('PYTHONPATH', 'alias'),)), install) == 'unknown'
    # The installation itself reached through a symlinked spelling.
    assert _kind(_record(env=(('PYTHONPATH', str(install)),)), link) == 'unknown'
    # Control: a symlink to somewhere else stays positively unrelated.
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    other = tmp_path / 'other-alias'
    other.symlink_to(elsewhere)
    assert _kind(_record(env=(('PYTHONPATH', str(other)),)), install) == 'unrelated'
    # Non-import references (argv words) are judged lexically, never by statting foreign paths.
    assert _kind(_record(argv=('python', str(link / 'main.py'))), install) == 'unrelated'


def test_relative_argv_and_flag_values_are_resolved_too(tmp_path):
    install = tmp_path / 'work' / 'install'
    install.mkdir(parents=True)
    assert _kind(_record(cwd=str(tmp_path / 'work'), argv=('python', 'install/main.py')), install) == 'unknown'
    assert _kind(_record(cwd=str(tmp_path / 'work' / 'x'), argv=('python', '../install/main.py')),
                 install) == 'unknown'
    assert _kind(_record(argv=('python', f'--root={install}')), install) == 'unknown'
    assert _kind(_record(argv=('python', '--root=/elsewhere')), install) == 'unrelated'


def test_unanchorable_relative_reference_is_uncertain_not_excluded(tmp_path):
    install = tmp_path / 'install'
    install.mkdir()
    deleted = f'{tmp_path}/gone (deleted)'
    # '..' under a deleted cwd could climb anywhere: uncertain.
    assert _kind(_record(cwd=deleted, env=(('PYTHONPATH', '../install'),)), install) == 'unknown'
    # Without '..', the value lies under the (foreign) deleted cwd: still positively foreign.
    assert _kind(_record(cwd=deleted, env=(('PYTHONPATH', f'a{os.pathsep}b'),)), install) == 'unrelated'


def test_oversized_search_path_cannot_be_proven_unrelated(tmp_path):
    install = tmp_path / 'install'
    install.mkdir()
    many = os.pathsep.join(f'/nowhere/{index}' for index in range(admission._MAX_PATH_LIST_ENTRIES + 1))
    assert _kind(_record(env=(('PYTHONPATH', many),)), install) == 'unknown'
    few = os.pathsep.join(f'/nowhere/{index}' for index in range(admission._MAX_PATH_LIST_ENTRIES))
    assert _kind(_record(env=(('PYTHONPATH', few),)), install) == 'unrelated'


def test_unresolvable_scope_roots_never_exclude():
    # No usable root => nothing to exclude against: classification reports no proof of absence either way.
    assert admission._record_touches(_record(), '', 'relative/root') is False


def test_overlong_reference_cannot_name_the_installation_and_resolution_errors_are_uncertain(tmp_path, monkeypatch):
    install = tmp_path / 'install'
    install.mkdir()
    overlong = '/x/' + 'a' * (admission._MAX_PATH_BYTES + 1)
    assert _kind(_record(env=(('PYTHONPATH', overlong),)), install) == 'unrelated'

    def refuse(path):
        raise OSError('no access')
    monkeypatch.setattr(admission.os.path, 'realpath', refuse)
    # An import search path that cannot be resolved is uncertain, never excluded...
    assert _kind(_record(env=(('PYTHONPATH', '/somewhere/else'),)), install) == 'unknown'
    # ...and so is an unresolvable root itself.
    assert admission._record_touches(_record(), str(install)) is True


@pytest.mark.parametrize('multiple', [False, True])
def test_real_process_importing_installation_via_pythonpath_is_not_unrelated(tmp_path, multiple):
    """Review repro: foreign cwd/script/home, real import from this checkout via PYTHONPATH."""
    script = tmp_path / 'importer.py'
    script.write_text('import sys\nfrom hermes_cli import serve_producer_protocol\n'
                      'print(serve_producer_protocol.__file__, flush=True)\nsys.stdin.readline()\n')
    path = str(ROOT) + (os.pathsep + str(tmp_path) if multiple else '')
    child = subprocess.Popen([sys.executable, str(script)], cwd=tmp_path, text=True,
                             env=dict(os.environ, HERMES_HOME=str(tmp_path / 'other-home'), PYTHONPATH=path),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert select.select([child.stdout], [], [], 30)[0]
        assert child.stdout.readline().strip() == str(ROOT / 'hermes_cli/serve_producer_protocol.py')
        adapter = LinuxProcessCensus()
        record = adapter.read_process(child.pid, adapter.boot_id())
        result = admission.classify_process(record, (), 'generation', installation=str(ROOT),
                                            home=str(tmp_path / 'scope-home'))
        assert result.kind != 'unrelated', (result, record.environment)
        assert result.kind == 'unknown'
    finally:
        child.communicate(input='done\n', timeout=30)


# ---- nonblocking, descriptor-typed raw file reads --------------------------------------

def _bounded(call, fifo: Path | None = None, timeout: float = 10):
    """Run ``call`` in a daemon thread; a hang is released and reported, never inherited."""
    outcome = {}

    def run():
        try:
            outcome['value'] = call()
        except BaseException as exc:  # the thread reports every outcome
            outcome['error'] = exc
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        if fifo is not None:  # unblock the stuck open so no thread outlives the test
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
            worker.join(5)
        pytest.fail('raw-file read blocked on a non-regular file')
    return outcome


def test_read_bytes_refuses_fifo_without_blocking(tmp_path):
    fifo = tmp_path / 'ledger'
    os.mkfifo(fifo, 0o600)
    outcome = _bounded(lambda: census._read_bytes(fifo, 1024), fifo)
    assert isinstance(outcome.get('error'), CensusUnavailable), outcome
    assert 'nonregular' in str(outcome['error'])


@pytest.mark.parametrize('device', ['/dev/null', '/dev/zero'])
def test_read_bytes_refuses_character_devices(device):
    with pytest.raises(CensusUnavailable, match='nonregular'):
        census._read_bytes(Path(device), 1024)


def test_read_bytes_nofollow_refuses_any_symlink_but_default_reads_procfs(tmp_path):
    target = tmp_path / 'real'
    target.write_bytes(b'[]')
    link = tmp_path / 'link'
    link.symlink_to(target)
    with pytest.raises(OSError):
        census._read_bytes(link, 16, nofollow=True)
    assert census._read_bytes(target, 16, nofollow=True) == b'[]'
    # Control: the nonblocking descriptor read still serves real procfs identity files.
    assert census._read_bytes(Path('/proc/self/stat'), 8192).split(b' ', 1)[0] == str(os.getpid()).encode()
    with pytest.raises(CensusUnavailable, match='budget'):
        census._read_bytes(target, 1)


def test_read_bytes_closes_descriptor_on_refusal():
    before = len(os.listdir('/proc/self/fd'))
    for _ in range(20):
        with pytest.raises(CensusUnavailable):
            census._read_bytes(Path('/dev/null'), 8)
    assert len(os.listdir('/proc/self/fd')) == before


@pytest.mark.parametrize('kind', ['fifo', 'symlink_to_fifo', 'symlink_to_device', 'symlink_to_regular', 'directory'])
def test_unsafe_main_ledger_is_uncertainty_not_a_hang(tmp_path, kind):
    path = tmp_path / 'spawn-ledger.json'
    regular = tmp_path / 'regular.json'
    regular.write_text('[]')
    fifo = tmp_path / 'fifo'
    os.mkfifo(fifo, 0o600)
    {'fifo': lambda: os.mkfifo(path, 0o600), 'symlink_to_fifo': lambda: path.symlink_to(fifo),
     'symlink_to_device': lambda: path.symlink_to('/dev/zero'),
     'symlink_to_regular': lambda: path.symlink_to(regular), 'directory': path.mkdir}[kind]()
    release = path if kind == 'fifo' else fifo
    outcome = _bounded(lambda: admission.collect_raw_ledger(tmp_path), release)
    assert isinstance(outcome.get('error'), CensusUnavailable), outcome
    with pytest.raises(CensusUnavailable):
        census.read_ledger_strict(path)  # also refused beneath the collector's own lstat check


def test_regular_ledger_control_is_still_positive_evidence(tmp_path):
    (tmp_path / 'spawn-ledger.json').write_text('[]')
    evidence = admission.collect_raw_ledger(tmp_path)
    assert evidence.refs == (f'ledger:{tmp_path / "spawn-ledger.json"}:0',) and not evidence.conflicts


@pytest.mark.parametrize('kind', ['fifo', 'symlink_to_fifo', 'symlink_to_regular'])
def test_unsafe_pending_row_is_uncertainty_not_a_hang(tmp_path, kind):
    ledger = tmp_path / 'spawn-ledger.json'
    ledger.write_text('[]')
    pending = tmp_path / 'spawn-ledger.json.pending'
    pending.mkdir()
    entry = pending / 'row.json'
    fifo = tmp_path / 'fifo'
    os.mkfifo(fifo, 0o600)
    real = tmp_path / 'real.json'
    real.write_text('{}')
    {'fifo': lambda: os.mkfifo(entry, 0o600), 'symlink_to_fifo': lambda: entry.symlink_to(fifo),
     'symlink_to_regular': lambda: entry.symlink_to(real)}[kind]()
    outcome = _bounded(lambda: admission.collect_raw_ledger(tmp_path), entry if kind == 'fifo' else fifo)
    assert isinstance(outcome.get('error'), CensusUnavailable), outcome


def test_pending_directory_that_is_a_fifo_is_uncertainty_not_a_hang(tmp_path):
    (tmp_path / 'spawn-ledger.json').write_text('[]')
    pending = tmp_path / 'spawn-ledger.json.pending'
    os.mkfifo(pending, 0o600)
    outcome = _bounded(lambda: admission.collect_raw_ledger(tmp_path), pending)
    assert isinstance(outcome.get('error'), CensusUnavailable), outcome


def test_ledger_swapped_for_fifo_between_lstat_and_open_is_not_a_hang(tmp_path, monkeypatch):
    ledger = tmp_path / 'spawn-ledger.json'
    ledger.write_text('[]')
    real_open, swapped = os.open, []

    def racing_open(path, flags, *args, **kwargs):
        if Path(path) == ledger and not swapped:
            swapped.append(True)  # lstat already judged a regular file; swap before the open
            ledger.unlink()
            os.mkfifo(ledger, 0o600)
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(census.os, 'open', racing_open)
    outcome = _bounded(lambda: admission.collect_raw_ledger(tmp_path), ledger)
    assert swapped
    assert isinstance(outcome.get('error'), CensusUnavailable), outcome
    assert 'nonregular' in str(outcome['error'].__cause__ or outcome['error']) or 'ledger' in str(outcome['error'])


def test_participant_ledger_swapped_after_lstat_is_refused_not_blocking(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    admission.enroll_launch(runtime, 'serve', installation=ROOT)
    path = runtime / 'participants.json'
    real_open, swapped = os.open, []

    def racing_open(target, flags, *args, **kwargs):
        if Path(target) == path and not swapped:
            swapped.append(True)
            path.unlink()
            os.mkfifo(path, 0o600)
        return real_open(target, flags, *args, **kwargs)
    monkeypatch.setattr(census.os, 'open', racing_open)
    outcome = _bounded(lambda: admission.read_participants_strict(runtime), path)
    assert swapped and isinstance(outcome.get('error'), CensusUnavailable), outcome


def test_fifo_ledger_does_not_hold_the_launch_gate(tmp_path):
    """Review repro: the real collector inside a held gate returns, and the gate is then free."""
    home = tmp_path / 'home'
    home.mkdir()
    os.mkfifo(home / 'spawn-ledger.json', 0o600)
    runtime = tmp_path / 'runtime'
    script = tmp_path / 'reader.py'
    script.write_text('import sys\nfrom pathlib import Path\n'
                      'from hermes_cli.serve_producer_admission import LaunchGate, collect_raw_ledger\n'
                      'from hermes_cli.serve_producer_census import CensusUnavailable\n'
                      'with LaunchGate.acquire(Path(sys.argv[1])):\n'
                      "    print('gate-held', flush=True)\n"
                      '    try:\n        collect_raw_ledger(Path(sys.argv[2]))\n'
                      "    except CensusUnavailable as exc:\n        print('incomplete', exc, flush=True)\n"
                      "    else:\n        raise SystemExit(3)\n")
    child = subprocess.Popen([sys.executable, str(script), str(runtime), str(home)], text=True,
                             env=dict(os.environ, PYTHONPATH=str(ROOT)),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert select.select([child.stdout], [], [], 30)[0]
        assert child.stdout.readline().strip() == 'gate-held'
        try:
            _, err = child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            with pytest.raises(CustodyConflict):
                admission.LaunchGate.acquire(runtime)
            pytest.fail('FIFO ledger blocks while retaining the shared launch gate')
        assert child.returncode == 0, err  # the collector reported CensusUnavailable and the gate was released
        admission.LaunchGate.acquire(runtime).close()
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)


def test_gate_inspection_with_fifo_ledger_is_incomplete_and_returns(tmp_path, monkeypatch):
    """The real host collectors inside a held gate (synthetic empty census: no real-home /proc scan)."""
    home = tmp_path / 'home'
    home.mkdir()
    os.mkfifo(home / 'spawn-ledger.json', 0o600)
    monkeypatch.setenv('HERMES_GATEWAY_LOCK_DIR', str(tmp_path / 'locks'))  # never read the account's locks

    def inspect():
        with admission.LaunchGate.acquire(tmp_path / 'runtime') as gate:
            return gate.inspect(
                boot_id='boot', generation='generation', installation=str(ROOT),
                census=lambda: census.ProcessCensus('boot', 1.0, 2.0, (), (), ()),
                collectors=admission.host_collectors(home, ROOT),
                classify=lambda record: None, legacy_candidates=(), exited=lambda identity: False)
    outcome = _bounded(inspect, home / 'spawn-ledger.json', timeout=60)
    cut = outcome.get('value')
    assert cut is not None, outcome
    assert not cut.complete
    assert any(error.startswith('raw_ledger:CensusUnavailable') for error in cut.errors), cut.errors
