"""Actual Board HTTP/DO fixture and real bounded Hub HTTP consumer.

Fixture is captured from owner-release.test.ts public executor test; the HTTP peer
below replays those bytes, not a composed DO/HOME/executor proof.
"""
from __future__ import annotations

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hermes_cli import kanban_release_public as subject
from hermes_cli.kanban_sync_client import Hub, Identity
from hermes_cli.kanban_sync_hub_transport import BoundedResponseTooLarge


def fixture():
    return json.loads((Path(__file__).parents[1]/'fixtures'/'owner_release_executor_board.json').read_text())


def validate(value):
    return subject.evidence(value,board=value['board'],task=value['row']['task_id'],uid=value['uid'])


def test_actual_board_producer_contract():
    value=fixture()
    public,got=validate(value)
    assert got['result']['outcome']=='released'
    assert 'consumption' not in got
    assert set(public)=={'version','board','reader_gateway','intent','renewal','delivery','events'}
    assert len(got['effects'])==4


@pytest.mark.parametrize('damage',['version','actor','reader','sequence','duplicate','effect','carrier','result','canonical','origin','extra'])
def test_invalid_wire_never_certifies(damage):
    value=fixture();p=value['owner_release_applied_v1']
    if damage=='version':p['version']=True
    if damage=='actor':p['intent']['actor']['gateway']=p['intent']['home']
    if damage=='reader':p['reader_gateway']=p['intent']['home']
    if damage=='sequence':p['events'].reverse()
    if damage=='duplicate':p['delivery']['uid']=p['renewal']['uid']
    if damage=='effect':next(row for row in p['events'] if row['kind']=='stop_released')['payload']='{}'
    if damage=='carrier':p['delivery']['payload']='{}'
    if damage=='result':p['events'][-1]['payload']='{}'
    if damage=='canonical':value['row']['payload']='{}'
    if damage=='origin':p['events'][0]['origin_gateway']='other'
    if damage=='extra':p['consumption']={}
    with pytest.raises(ValueError):validate(value)


def test_real_hub_row_evidence_bounded_whole_response_and_auth():
    value=fixture();raw=json.dumps(value).encode();requests=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path,self.headers.get('Authorization')))
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(raw)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    hub=Hub(Identity(url=f'http://127.0.0.1:{server.server_port}',key='fixture-key',gateway=value['owner_release_applied_v1']['reader_gateway']))
    try:
        got=hub.row_evidence(value['board'],'task_events',value['uid'],max_response_bytes=subject.MAX_RESPONSE_BYTES)
        assert got==value and validate(got)[1]['result']['outcome']=='released'
        assert requests[0][1]=='Bearer fixture-key'
        assert '/evidence?table=task_events&uid=' in requests[0][0]
        with pytest.raises(BoundedResponseTooLarge):
            hub.row_evidence(value['board'],'task_events',value['uid'],max_response_bytes=100)
    finally:
        hub.close();server.shutdown();server.server_close();thread.join(5)
        assert not thread.is_alive()


def test_complete_escaped_envelope_bound_is_separate_from_source_cap():
    value=fixture();p=value['owner_release_applied_v1']
    # Existing public delivery inventory can dominate nested encoding. It remains
    # public history, not private consumption; exercise full escaped raw payload.
    cp=json.loads(p['delivery']['payload']);cp['seen_comments']=[{'body':'\x01'*128000}]
    p['delivery']['payload']=json.dumps(cp)
    raw=json.dumps(value).encode()
    assert len(raw)>768000 and len(raw)<subject.MAX_RESPONSE_BYTES
    assert validate(value)[1]['result']['outcome']=='released'
    oversized=copy.deepcopy(p);oversized['delivery']['payload']='x'*(subject.MAX_BYTES+1)
    with pytest.raises(ValueError,match='oversized'):
        subject.validate(oversized,board=value['board'],task=value['row']['task_id'],uid=value['uid'])
