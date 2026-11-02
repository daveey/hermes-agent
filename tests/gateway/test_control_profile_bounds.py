"""Wire-budget contract, including names, frames and the request envelope."""
import json

from gateway.control_socket import GatewayControlServer, _MAX_RESPONSE_BYTES


def test_profile_trims_whole_envelope_without_corrupting_shape(tmp_path):
    frame = 'huge-path/' * 100000 + ':1:function'
    row = dict(name='名' * 100000, native_id=12, cpu_s=0.1, samples=1,
               first_seen_sample=0, last_seen_sample=0,
               top_self=[[frame, 1]], top_inclusive=[[frame, 1]])
    normal = {**row, 'name': 'normal'}
    payload = dict(seconds=1, hz=100, samples=1, threads=[row, normal])
    server = GatewayControlServer(tmp_path, verb_handlers={'profile': lambda: payload})
    request_id = 'long-id' * 8000
    raw = server.handle_request_line(json.dumps({'verb': 'profile', 'id': request_id}).encode())
    assert len(raw) < _MAX_RESPONSE_BYTES
    response = json.loads(raw)
    assert response['ok'], response
    assert response['id'] == request_id
    result = response['result']
    assert result['truncated'] is True
    assert isinstance(result['threads'], list)
    assert [t['name'] for t in result['threads']] == ['normal']
    assert result['omitted_threads'] == 1 and result['omitted_frames'] == 4
    for thread in result['threads']:
        assert set(row) <= set(thread)
        for field in ('top_self', 'top_inclusive'):
            assert all(isinstance(pair, list) and len(pair) == 2 for pair in thread[field])
