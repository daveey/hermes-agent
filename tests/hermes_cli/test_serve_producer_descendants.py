"""Direct-child exit must not permit production over a surviving fork/exec child."""
import hashlib
import os
from pathlib import Path

import pytest

from hermes_cli.serve_producer_custody import CustodyConflict, FamilyCustody
from hermes_cli.serve_producer_runtime import ProjectionRelay, projection_response
from tests.waiting import wait_until


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('exec_descendant', [False, True])
def test_restart_refuses_before_provider_import_while_descendant_holds_custody(tmp_path, exec_descendant, capfd):
    # The child drops only transport, as in the independent review probe. The
    # exec arm also exercises custody inheritance across a real process image.
    descendant_source = '''import os, select, sys
from pathlib import Path
home = Path(sys.argv[1])
(home / 'descendant').write_text(str(os.getpid()), encoding='ascii')
while not (home / 'release').exists():
    select.select([], [], [], .01)
'''
    (tmp_path / 'descendant.py').write_text(descendant_source, encoding='utf-8')
    source = '''import os, select, sys
from pathlib import Path
home = Path(__file__).parent
with (home / 'imports').open('a', encoding='ascii') as stream:
    stream.write('imported\\n')
def produce(payload):
    if (home / 'forked').exists():
        return {'overlap': True}
    (home / 'forked').touch()
    pid = os.fork()
    if pid == 0:
        os.close(int(sys.argv[2]))
        if payload['exec']:
            os.execl(sys.executable, sys.executable, '-I', str(home / 'descendant.py'), str(home))
        (home / 'descendant').write_text(str(os.getpid()), encoding='ascii')
        while not (home / 'release').exists():
            select.select([], [], [], .01)
        os._exit(0)
    while not (home / 'descendant').exists():
        select.select([], [], [], .01)
    os._exit(17)
'''
    provider = tmp_path / 'provider.py'
    provider.write_text(source, encoding='utf-8')
    runtime = tmp_path / 'runtime'
    relay = ProjectionRelay({'path': str(provider), 'sha256': hashlib.sha256(source.encode()).hexdigest()},
                            home=tmp_path, runtime=runtime, generation='descendants')
    try:
        relay.start_for_fixture()
        key = relay.submit({'exec': exec_descendant})
        assert wait_until(lambda: relay.error is not None or relay.read(key) is not None, timeout=15)
        descendant = int((tmp_path / 'descendant').read_text(encoding='ascii'))
        os.kill(descendant, 0)
        fdinfo = Path(f'/proc/{descendant}/fdinfo')
        held = {os.stat(f'/proc/{descendant}/fd/{entry.name}').st_ino
                for entry in fdinfo.iterdir() if 'FLOCK' in entry.read_text(encoding='ascii')}
        assert (runtime / 'panel.lock').stat().st_ino in held
        assert (runtime / 'panel-execution.lock').stat().st_ino in held
        assert relay.read(key) is None, 'replacement produced over surviving family work'
        assert not relay.acknowledged(key)
        assert relay.error == 'EOFError'
        assert (tmp_path / 'imports').read_text(encoding='ascii') == 'imported\n'
        assert projection_response(relay, {'sessions': ['fixture']}).status_code == 503
        assert relay.close(timeout=10)
        # The broker has exited, but neither in-process nor external successors
        # may take the surviving descendant's actual panel fence.
        with pytest.raises(CustodyConflict):
            FamilyCustody.acquire(runtime, 'panel')
        with pytest.raises(CustodyConflict):
            FamilyCustody.acquire_execution(runtime, 'panel')
    finally:
        (tmp_path / 'release').touch()
        assert relay.close(timeout=10)

    def released():
        try:
            successor = FamilyCustody.acquire(runtime, 'panel')
        except CustodyConflict:
            return False
        successor.close()
        return True

    assert wait_until(released, timeout=10)
    assert 'Traceback' not in capfd.readouterr().err
    # Uncertain retirement refused this relay; a fresh relay after actual drain
    # can import and publish normally without weakening the surviving lock.
    successor = ProjectionRelay(relay.config['provider'], home=tmp_path, runtime=runtime, generation='after-drain')
    try:
        successor.start_for_fixture()
        key = successor.submit({'exec': exec_descendant})
        assert wait_until(lambda: successor.acknowledged(key), timeout=10)
    finally:
        assert successor.close(timeout=10)


@pytest.mark.platforms('posix')
def test_execution_acquisition_keeps_independent_inheritable_description(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    broker = FamilyCustody.acquire(runtime, 'panel')
    try:
        execution = FamilyCustody.acquire_execution(runtime, 'panel')
        assert os.get_inheritable(execution.fd)
        with pytest.raises(CustodyConflict):
            FamilyCustody.acquire_execution(runtime, 'panel')
        execution.close()
        # Failed inheritance setup must close its newly acquired description,
        # not leak a lock that bricks every future producer in this runtime.
        with monkeypatch.context() as patch:
            def refuse_inheritance(fd, value):
                raise OSError('fixture inheritance refused')
            patch.setattr(os, 'set_inheritable', refuse_inheritance)
            with pytest.raises(OSError, match='fixture inheritance refused'):
                FamilyCustody.acquire_execution(runtime, 'panel')
        recovered = FamilyCustody.acquire_execution(runtime, 'panel')
        recovered.close()
        with pytest.raises(ValueError, match='unknown producer family'):
            FamilyCustody.acquire_execution(runtime, 'not-a-family')
    finally:
        broker.close()
