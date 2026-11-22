"""Coalesce metadata ACK clears without ever sending an unjournaled mutation."""
import json

from agent.runtime_session_store import RuntimeSessionStore


def test_metadata_ack_clear_coalesces_into_next_pending_save(tmp_path, monkeypatch):
    seen, saves = [], []
    def rpc(method, **params):
        disk = json.loads(store.path.read_text())
        assert disk['pending'][0]['sequence'] == params['sequence']
        assert disk['pending'][0]['payload'] == params['payload']
        seen.append(params['sequence'])
        return {}
    store = RuntimeSessionStore(rpc, {'session_id': 's'}, tmp_path / 'outbox')
    original = store._save
    def save(journal):
        saves.append(json.loads(json.dumps(journal)))
        original(journal)
    monkeypatch.setattr(store, '_save', save)
    try:
        store.update_session_tool_names('s', ['terminal'])
        store.update_session_tool_names('s', ['terminal', 'read_file'])
        assert seen == [1, 2]
        assert len(saves) == 2  # before-send durability only; ACK piggybacks on the next save
        assert saves[1]['acked'][0]['sequence'] == 1
        assert store.journal['pending'] == []
        assert json.loads(store.path.read_text())['pending'][0]['sequence'] == 2
    finally:
        store.close()
    assert json.loads(store.path.read_text())['pending'] == []
