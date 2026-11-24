"""Independent review probes; disposable local fixtures only."""
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('fifo', [False, True])
def test_fifo_ledger_does_not_hold_launch_gate_forever(tmp_path, fifo):
    root = tmp_path / 'home'
    root.mkdir()
    if fifo:
        os.mkfifo(root / 'spawn-ledger.json', 0o600)
    else:
        (root / 'spawn-ledger.json').write_text('[]')
    runtime = tmp_path / 'runtime'
    script = tmp_path / 'reader.py'
    script.write_text('''import sys
from pathlib import Path
from hermes_cli.serve_producer_admission import LaunchGate, collect_raw_ledger
with LaunchGate.acquire(Path(sys.argv[1])):
    print('gate-held', flush=True)
    collect_raw_ledger(Path(sys.argv[2]))
''')
    child = subprocess.Popen([sys.executable, str(script), str(runtime), str(root)],
                             env=dict(os.environ, PYTHONPATH=str(Path.cwd())),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    timed_out = False
    try:
        assert select.select([child.stdout], [], [], 10)[0]
        assert child.stdout.readline().strip() == 'gate-held'
        try:
            child.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            timed_out = True
            from hermes_cli.serve_producer_admission import LaunchGate
            from hermes_cli.serve_producer_custody import CustodyConflict
            with pytest.raises(CustodyConflict):
                LaunchGate.acquire(runtime)
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)
    assert not timed_out, 'FIFO ledger blocks while retaining the shared launch gate'


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('multiple', [False, True])
def test_colon_pythonpath_import_is_not_unrelated(tmp_path, multiple):
    installation = Path.cwd()
    script = tmp_path / 'importer.py'
    script.write_text('''import sys
from hermes_cli import serve_producer_protocol
print(serve_producer_protocol.__file__, flush=True)
sys.stdin.readline()
''')
    child = subprocess.Popen([sys.executable, str(script)], cwd=tmp_path,
        env=dict(os.environ, HERMES_HOME=str(tmp_path / 'other-home'),
                 PYTHONPATH=str(installation) + (os.pathsep + str(tmp_path) if multiple else '')),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([child.stdout], [], [], 10)[0]
        assert child.stdout.readline().strip() == str(installation / 'hermes_cli/serve_producer_protocol.py')
        from hermes_cli.serve_producer_census import LinuxProcessCensus
        from hermes_cli.serve_producer_admission import classify_process
        adapter = LinuxProcessCensus()
        record = adapter.read_process(child.pid, adapter.boot_id())
        result = classify_process(record, (), 'generation', installation=str(installation), home=str(tmp_path / 'scope-home'))
        assert result.kind != 'unrelated', (result, record.environment)
    finally:
        child.communicate(input='done\n', timeout=10)
