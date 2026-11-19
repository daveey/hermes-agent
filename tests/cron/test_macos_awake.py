"""Native ABI ownership and power-policy parity; native smoke runs on macOS CI."""
import ctypes as C
from types import SimpleNamespace

import pytest

from cron import macos_awake as native


class Function:
    def __init__(self, fn):
        self.fn = fn
    def __call__(self, *args):
        return self.fn(*args)


@pytest.fixture
def api(monkeypatch):
    state = SimpleNamespace(active=0, ac=b'AC Power', closed=False, error=None,
                            released=[], entries=[], calls=[], keys={}, next_key=100)
    def key(_allocator, text, _encoding):
        state.next_key += 1
        state.keys[state.next_key] = text
        return 0 if state.error == 'key' else state.next_key
    def assertions(out):
        state.calls.append('assertions')
        C.cast(out, C.POINTER(C.c_void_p))[0] = 0 if state.error == 'null_assertions' else 10
        return 1 if state.error == 'assertions' else 0
    def number(_value, kind, out):
        assert kind == 3
        C.cast(out, C.POINTER(C.c_int32))[0] = state.active
        return state.error != 'number'
    def text(_value, buf, _size, _encoding):
        buf.value = state.ac
        return state.error != 'text'
    def matching_service(port, matching):
        assert port == 0 and matching == 40
        state.calls.append('lid')
        return 0 if state.error == 'entry' else 41
    def type_id(value):
        return 99 if state.error == 'type' or (state.error == 'lid_type' and value == 42) else {11: 1, 42: 2}[value]
    cf = SimpleNamespace(**{name: Function(fn) for name, fn in {
        'CFRelease': state.released.append,
        'CFGetTypeID': type_id,
        'CFStringCreateWithCString': key,
        'CFDictionaryGetValue': lambda d, k: 0 if state.error == 'missing_assertion' else 11,
        'CFNumberGetTypeID': lambda: 1,
        'CFNumberGetValue': number,
        'CFBooleanGetTypeID': lambda: 2,
        'CFBooleanGetValue': lambda v: state.closed,
        'CFStringGetCString': text,
    }.items()})
    io = SimpleNamespace(**{name: Function(fn) for name, fn in {
        'IOPMCopyAssertionsStatus': assertions,
        'IOPSCopyPowerSourcesInfo': lambda: 0 if state.error == 'info' else 20,
        'IOPSGetProvidingPowerSourceType': lambda info: 0 if state.error == 'source' else 21,
        'IOServiceMatching': lambda name: 0 if state.error == 'matching' else 40,
        'IOServiceGetMatchingService': matching_service,
        'IORegistryEntryCreateCFProperty': lambda *args: 0 if state.error == 'missing_lid' else 42,
        'IOObjectRelease': state.entries.append,
    }.items()})
    monkeypatch.setattr(native.C, 'CDLL', lambda path: cf if 'CoreFoundation' in path else io)
    native._bindings.cache_clear()
    yield state, cf, io
    native._bindings.cache_clear()


@pytest.mark.parametrize('active,ac,closed,expected', [
    (1, b'Battery Power', True, True), (0, b'AC Power', False, True),
    (0, b'AC Power', True, False), (0, b'Battery Power', False, False),
    (0, b'UPS Power', False, False), (255, b'Battery Power', True, False),
])
def test_native_policy_and_owned_references(api, active, ac, closed, expected):
    state, cf, io = api
    state.active, state.ac, state.closed = active, ac, closed
    assert native.fully_awake() is expected
    assert state.released.count(10) == 1
    assert 11 not in state.released and 21 not in state.released  # borrowed
    assert all(state.released.count(k) == 1 for k in state.keys)
    if active == 1:
        assert state.calls == ['assertions']
        assert 20 not in state.released
    else:
        assert state.released.count(20) == 1
        if ac == b'AC Power':
            assert state.released.count(42) == 1 and state.entries == [41]
    assert io.IOPMCopyAssertionsStatus.argtypes == [C.POINTER(C.c_void_p)]
    assert io.IOPSCopyPowerSourcesInfo.restype is C.c_void_p
    assert io.IOServiceGetMatchingService.restype is C.c_uint32
    assert cf.CFNumberGetValue.argtypes == [C.c_void_p, C.c_long, C.c_void_p]


@pytest.mark.parametrize('error', ['assertions', 'key', 'type', 'number', 'info', 'source',
                                 'text', 'matching', 'entry', 'null_assertions', 'lid_type'])
def test_native_read_errors_release_and_propagate(api, error):
    state, _cf, _io = api
    state.error = error
    # A missing number type guard must not be masked by the later lid guard.
    # With an active value, the mutant would return before examining the lid.
    state.active = 1 if error == 'type' else 0
    with pytest.raises(OSError):
        native.fully_awake()
    assert state.released.count(10) == int(error != 'null_assertions')
    assert len(state.released) == len(set(state.released))
    if 41 in state.entries:
        assert state.entries == [41]


@pytest.mark.parametrize('error', ['missing_assertion', 'missing_lid'])
def test_missing_optional_state_matches_command_policy(api, error):
    state, _cf, _io = api
    state.error = error
    assert native.fully_awake()


def test_bindings_only_are_cached(api):
    state, _cf, _io = api
    power = native._bindings()
    assert native.fully_awake()
    state.closed = True
    assert not native.fully_awake()
    state.active = 1
    assert native.fully_awake()
    assert native._bindings() is power
    assert state.calls.count('assertions') == 3


@pytest.mark.parametrize('failure', [OSError('missing library'), AttributeError('missing symbol')])
def test_unavailable_api_selects_fallback(monkeypatch, failure):
    def missing():
        raise failure
    monkeypatch.setattr(native, '_Power', missing)
    native._bindings.cache_clear()
    try:
        assert native.fully_awake() is None
    finally:
        native._bindings.cache_clear()


@pytest.mark.platforms('macos')
def test_actual_native_power_read_never_spawns(monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('native query spawned'))
    native._bindings.cache_clear()
    try:
        assert native._bindings() is not None
        assert isinstance(native.fully_awake(), bool)
    finally:
        native._bindings.cache_clear()
