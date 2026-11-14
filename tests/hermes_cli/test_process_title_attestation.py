"""Exercise the real startup producer without loading or calling a host library."""
import pytest

from tests.hermes_cli.test_code_attestation import run_probe


@pytest.mark.parametrize("system, expected", [
    ("Darwin", "/usr/lib/libc.dylib"), ("Linux", "libc.so.6"), ("Windows", None),
])
def test_process_title_native_provenance(tmp_path, system, expected):
    result = run_probe(tmp_path, '''
        import ctypes, platform
        from hermes_cli.main import _set_process_title
        # Force the documented fallback even if the optional package is installed.
        sys.modules['setproctitle'] = None
        platform.system = lambda: SYSTEM
        calls = []
        class Library:
            def pthread_setname_np(self, title):
                calls.append(['pthread_setname_np', title.decode()])
            def prctl(self, *args):
                calls.append(['prctl', args[0], args[1].decode(), *args[2:]])
        def load(name, *, use_errno):
            calls.append(['load', name, use_errno])
            # Same event as ctypes' native dlopen; no real host OS calls.
            sys.audit('ctypes.dlopen', name)
            return Library()
        ctypes.CDLL = load
        before = set(a.quiet_unknown_partition()[0])
        _set_process_title()
        blocking, informational = a.quiet_unknown_partition()
        print(json.dumps(dict(calls=calls, blocking=sorted(set(blocking)-before),
                             informational=informational, raw=a.latched_unknowns())))
    '''.replace('SYSTEM', repr(system)))
    if system == 'Darwin':
        assert not any(r.startswith('native-load:') for r in result['blocking']), result
    if expected is None:
        assert result['calls'] == []
    else:
        assert result['calls'][0] == ['load', expected, True]
        if system == 'Darwin':
            assert result['calls'][1] == ['pthread_setname_np', 'hermes']
            assert 'native-load:' + expected in result['informational']
            assert 'native-load:' + expected in result['raw']
        else:
            assert result['calls'][1] == ['prctl', 15, 'hermes', 0, 0, 0]


def test_unresolved_and_invalid_native_names_still_block(tmp_path):
    result = run_probe(tmp_path, r'''
        names = ['libc.dylib', 'arbitrary.dylib', '/bad\x00/native.dylib']
        alias = pathlib.Path('core-alias').resolve()
        alias.symlink_to(root, target_is_directory=True)
        names.append(str(alias / 'native.dylib'))
        for name in names:
            sys.audit('ctypes.dlopen', name)
        blocking, informational = a.quiet_unknown_partition()
        print(json.dumps(dict(names=names, blocking=blocking, informational=informational)))
    ''')
    for name in result['names']:
        if '\x00' in name:
            assert 'audit-error:ValueError' in result['blocking']
        else:
            assert 'native-load:' + name in result['blocking']
        assert 'native-load:' + name not in result['informational']


def test_darwin_title_loader_failure_is_nonfatal(tmp_path):
    result = run_probe(tmp_path, '''
        import ctypes, platform
        from hermes_cli.main import _set_process_title
        sys.modules['setproctitle'] = None
        platform.system = lambda: 'Darwin'  # os-marker: ok — mocked loader; no host OS calls
        def fail(*args, **kwargs):
            raise OSError('fixture loader failure')
        ctypes.CDLL = fail
        _set_process_title()
        print(json.dumps({'survived': True}))
    ''')
    assert result['survived']
