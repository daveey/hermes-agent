"""Control socket ``profile`` verb: in-process sampling profiler of the live gateway daemon."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from gateway.control_socket import GatewayControlServer


@pytest.mark.parametrize('params', [
    {'seconds': 0}, {'seconds': 61}, {'seconds': float('nan')},
    {'seconds': float('inf')}, {'seconds': True}, {'seconds': '1'},
    {'hz': 9}, {'hz': 201}, {'hz': float('nan')}, {'hz': True},
    {'top': 0}, {'top': -1}, {'top': 1.5}, {'top': True},
])
def test_invalid_profile_parameters(params):
    from gateway.control_profile import validate_params
    with pytest.raises(ValueError):
        validate_params(params)


@pytest.mark.parametrize('params', [None, [], 'bad'])
def test_profile_rejects_nonobject_params(tmp_path, params, monkeypatch):
    monkeypatch.setattr('gateway.control_profile._collect', lambda *args: {})
    raw = json.dumps({'verb': 'profile', 'params': params}).encode()
    response = json.loads(GatewayControlServer(tmp_path).handle_request_line(raw))
    assert not response['ok'] and 'params' in response['error']


def test_parameter_defaults_and_boundaries():
    from gateway.control_profile import validate_params
    assert validate_params({}) == (20, 25, 40)
    assert validate_params({'seconds': 60, 'hz': 200, 'top': 1}) == (60, 200, 1)
    assert validate_params({'seconds': 1, 'hz': 10}) == (1, 10, 40)


@pytest.mark.parametrize('params, hz', [({'seconds': 1}, 25), ({'seconds': 1, 'hz': 50}, 50)])
def test_control_verb_omitted_hz_uses_default_and_explicit_hz_overrides(tmp_path, params, hz):
    raw = json.dumps({'verb': 'profile', 'params': params}).encode()
    response = json.loads(GatewayControlServer(tmp_path).handle_request_line(raw))
    assert response['ok'], response
    result = response['result']
    assert result['hz'] == hz and result['seconds'] == 1
    assert result['cpu_refresh_s'] == pytest.approx(0.1)
    # Ticks fall at start + k/hz before the 1 s deadline, so at most hz samples
    # (+1 for float rounding); the 50 Hz override must sample beyond 25 Hz's cap.
    assert 0 < result['samples'] <= hz + 1
    if hz == 50:
        assert result['samples'] > 26


def test_process_wide_concurrent_refusal_and_lock_release(tmp_path, monkeypatch):
    import gateway.control_profile as profiler
    started = threading.Event()
    release = threading.Event()
    collect = profiler._collect

    def blocked(*args):
        started.set()
        assert release.wait(5)
        return collect(*args)

    monkeypatch.setattr(profiler, '_collect', blocked)
    raw = json.dumps({'verb': 'profile', 'params': {'seconds': 1}}).encode()
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(GatewayControlServer(tmp_path).handle_request_line, raw)
        assert started.wait(5)
        try:
            before = time.monotonic()
            other = json.loads(GatewayControlServer(tmp_path).handle_request_line(raw))
            assert not other['ok'] and 'already' in other['error']
            assert time.monotonic() - before < 0.5
        finally:
            release.set()
        assert json.loads(first.result(5))['ok']
    assert json.loads(GatewayControlServer(tmp_path).handle_request_line(raw))['ok']


def _profile_burner_spin(stop):
    # Pure-Python loop with no Python-level calls, so every sample's leaf frame is this function.
    n = 0
    while not stop:
        n += 1
    return n


@pytest.mark.platforms("linux")
def test_profile_verb_reports_busy_thread_self_time_and_cpu(tmp_path):
    stop: list = []
    burner = threading.Thread(target=_profile_burner_spin, args=(stop,), name="profile-burner", daemon=True)
    burner.start()
    try:
        server = GatewayControlServer(tmp_path)
        raw = json.dumps({"verb": "profile", "id": 7, "protocol": 1,
                          "params": {"seconds": 1, "hz": 100}}).encode()
        response = json.loads(server.handle_request_line(raw))
    finally:
        stop.append(True)
        burner.join(5)

    assert response["ok"] is True, response
    assert response["id"] == 7
    result = response["result"]
    assert result["seconds"] == 1 and result["hz"] == 100
    assert result["samples"] > 0

    threads = {t["name"]: t for t in result["threads"]}
    busy = threads["profile-burner"]
    code = _profile_burner_spin.__code__
    key = f"{code.co_filename}:{code.co_firstlineno}:{code.co_name}"
    assert busy["top_self"][0][0] == key
    assert busy["top_self"][0][1] >= 25
    assert dict(busy["top_inclusive"])[key] == busy["top_self"][0][1]
    assert busy["cpu_s"] > 0
    assert busy["native_id"] == burner.native_id
    assert 0 <= busy["first_seen_sample"] <= busy["last_seen_sample"] < result["samples"]
