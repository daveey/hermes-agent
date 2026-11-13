"""Process-exit proof and killing mutants for the isolated DNS resolver."""
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_quota_recovery as recovery
from tools.environments.local import build_subprocess_env


@pytest.mark.parametrize('mutant', ['none', 'executor_dns', 'no_kill'])
def test_dns_deadline_in_fresh_interpreter_and_mutants(tmp_path, mutant):
    source = Path(recovery.__file__).read_text(encoding='utf-8')
    if mutant == 'executor_dns':
        source = source.replace('asyncio.get_running_loop().getaddrinfo = _getaddrinfo', 'pass')
    elif mutant == 'no_kill':
        source = source.replace('process.kill()', 'process.wait(timeout=10)')
    script = tmp_path / 'probe.py'
    marker = tmp_path / 'resolver-pid'
    started = tmp_path / 'call-started'  # excludes interpreter/import startup under load
    dns_script = f"import os,time; open({str(marker)!r}, 'w').write(str(os.getpid())); time.sleep(6)"
    script.write_text(
        'import sys, socket, time, types\n'
        f'sys.path.insert(0, {str(Path(recovery.__file__).resolve().parents[1])!r})\n'
        'import httpx\n'
        'import tools.environments.local  # loaded in production before any probe\n'
        'from types import SimpleNamespace\n'
        'module = types.ModuleType("isolated_recovery")\n'
        f'exec(compile({source!r}, "recovery.py", "exec"), module.__dict__)\n'
        'def stalled(*a, **kw):\n'
        '    time.sleep(6)\n'
        '    raise socket.gaierror("controlled DNS failure")\n'
        'socket.getaddrinfo = stalled\n'
        'module._client = lambda timeout: httpx.AsyncClient(timeout=timeout, trust_env=False)\n'
        f'module._DNS_SCRIPT = {dns_script!r}\n'
        f'open({str(started)!r}, "w").write(repr(time.time()))\n'
        'assert module.completion_succeeds("anthropic", "m", '
        'SimpleNamespace(runtime_api_key="synthetic"), time.monotonic()+0.5) is False\n',
        encoding='utf-8')
    result = subprocess.run([sys.executable, str(script)], capture_output=True,
                            text=True, encoding='utf-8', timeout=30, check=False,
                            env=build_subprocess_env())
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    # From the call to process exit: the deadline bounds both the call and shutdown.
    elapsed = time.time() - float(started.read_text())
    if mutant == 'none':
        assert elapsed < 4.0  # deadline 0.5s; slack for spawn+exit on a loaded CI runner
    else:
        assert elapsed > 5.5  # both mutants violate the same process-exit bound
    if marker.exists():
        assert not Path('/proc/' + marker.read_text()).exists(), 'resolver not reaped'


def test_resolver_roundtrips_addresses_without_network():
    import asyncio
    import socket

    rows = asyncio.run(recovery._getaddrinfo(b'127.0.0.1', 443, type=socket.SOCK_STREAM))
    assert rows
    assert all(row[0] == socket.AF_INET and row[4] == ('127.0.0.1', 443) for row in rows)


@pytest.mark.parametrize('child_exits_first', [False, True])
def test_resolver_failure_is_closed_and_reaped(monkeypatch, child_exits_first):
    import asyncio

    monkeypatch.setattr(recovery, '_DNS_SCRIPT', 'import sys; sys.exit(1)')
    if child_exits_first:
        # A starved parent can be scheduled after the child has already exited:
        # writing the request then hits EPIPE. Force that ordering deterministically.
        real_popen = subprocess.Popen

        def exited_popen(*args, **kw):
            process = real_popen(*args, **kw)
            process.wait(timeout=30)
            return process

        monkeypatch.setattr(subprocess, 'Popen', exited_popen)
    with pytest.raises(OSError, match='DNS resolution failed'):
        asyncio.run(recovery._getaddrinfo('unused.invalid', 443))


@pytest.mark.parametrize('drop_wait', [False, True])
def test_resolver_explicit_reap_guard_and_mutant(monkeypatch, drop_wait):
    import asyncio
    import io
    import types
    from unittest.mock import Mock

    source = Path(recovery.__file__).read_text(encoding='utf-8')
    if drop_wait:
        source = source.replace('process.wait(timeout=1)', 'pass')
    module = types.ModuleType('reap_proof')
    exec(compile(source, recovery.__file__, 'exec'), module.__dict__)
    process = types.SimpleNamespace(stdin=io.BytesIO(), poll=lambda: 0, returncode=0, wait=Mock())

    def spawn(*args, **kw):
        kw['stdout'].write(b'[]')
        return process

    monkeypatch.setattr(subprocess, 'Popen', spawn)
    assert asyncio.run(module._getaddrinfo('unused.invalid', 443)) == []
    assert process.wait.called is (not drop_wait)


@pytest.mark.parametrize('child_ok', [False, True])
def test_unreaped_resolver_timeout_never_escapes(monkeypatch, child_ok):
    """A resolver that outlives the 1 s reap is killed and reaped again; TimeoutExpired never escapes."""
    import asyncio
    import io
    import types
    from unittest.mock import Mock

    waits = Mock(side_effect=[subprocess.TimeoutExpired('resolver', 1), 0])
    process = types.SimpleNamespace(stdin=io.BytesIO(), poll=lambda: 0, returncode=0 if child_ok else 1,
                                    wait=waits, kill=Mock())

    def spawn(*args, **kw):
        kw['stdout'].write(b'[]')
        return process

    monkeypatch.setattr(subprocess, 'Popen', spawn)
    if child_ok:
        assert asyncio.run(recovery._getaddrinfo('unused.invalid', 443)) == []
    else:
        with pytest.raises(OSError, match='DNS resolution failed'):
            asyncio.run(recovery._getaddrinfo('unused.invalid', 443))
    assert waits.call_count == 2
    process.kill.assert_called_once_with()


def test_completion_returns_false_when_resolver_spawn_raises_subprocess_error(monkeypatch):
    """A SubprocessError from the resolver spawn (not an OSError) still reads as a failed proof."""
    import httpx
    import types

    def spawn(*args, **kwargs):
        raise subprocess.SubprocessError('resolver spawn failed')

    monkeypatch.setattr(subprocess, 'Popen', spawn)
    monkeypatch.setattr(recovery, '_client', lambda timeout: httpx.AsyncClient(timeout=timeout, trust_env=False))
    entry = types.SimpleNamespace(runtime_api_key='synthetic')
    assert recovery.completion_succeeds('anthropic', 'm', entry, time.monotonic() + 5) is False


def test_completion_returns_false_when_resolver_cannot_be_reaped(monkeypatch):
    import httpx
    import io
    import types
    from unittest.mock import Mock

    process = types.SimpleNamespace(
        stdin=io.BytesIO(), poll=lambda: 0, returncode=1, kill=Mock(),
        wait=Mock(side_effect=subprocess.TimeoutExpired('resolver', 1)))
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **kw: process)
    monkeypatch.setattr(recovery, '_client', lambda timeout: httpx.AsyncClient(timeout=timeout, trust_env=False))
    entry = types.SimpleNamespace(runtime_api_key='synthetic')
    assert recovery.completion_succeeds('anthropic', 'm', entry, time.monotonic() + 5) is False
    assert process.wait.call_count >= 2
