"""Raw receipt races cannot certify a normalized holder COUNTS memo."""
import sqlite3

import pytest

from hermes_cli import kanban_annotation_counts as counts
from hermes_cli import kanban_annotation_hub_key as hk
from hermes_cli import kanban_annotation_parts as parts
from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_annotation_counts_custody as custody
from tests.hermes_cli import test_annotation_heartbeat_fence as fence
from tests.hermes_cli import test_kanban_annotation_parts as holders

world = custody.world
clock = custody.clock
replica = fence.replica


def test_aba_during_detached_reuse_forces_read_and_refuses_certificate(world, clock, replica, monkeypatch):
    _state, fake = clock
    path, _conn, beat = replica
    monkeypatch.setattr(ksa, '_hub_db_path', lambda: path)
    beat(fence.info(fake.time() - 1))
    holders._refresh()
    original = counts._detach
    before = world.counts
    fired = []
    def crossing(value):
        result = original(value)
        if not fired:
            fired.append(True)
            beat(fence.info(fake.time() + 10))
            beat(fence.info(fake.time() - 1))
        return result
    monkeypatch.setattr(counts, '_detach', crossing)
    world.declared.write_text('attention')
    holders._refresh()
    assert fired and world.counts == before + 1
    assert holders.SCOPE not in parts._seen and holders.SCOPE not in counts._entries
    holders._refresh()
    assert world.counts == before + 2 and holders.SCOPE in counts._entries


def test_original_receipt_rejects_main_file_replacement(replica, tmp_path):
    path, _conn, _beat = replica
    _key, receipt = hk.capture(path, fence.NOW)
    replacement = tmp_path / 'replacement.db'
    with sqlite3.connect(replacement) as conn:
        conn.execute('CREATE TABLE other(value)')
    path.rename(tmp_path / 'old-hub.db')
    replacement.replace(path)
    assert not receipt.valid()


def test_acquisition_rejects_aba_before_returning_key(replica, monkeypatch):
    path, _conn, beat = replica
    original = hk._stamp
    calls = []
    def racing(conn, target):
        calls.append(True)
        if len(calls) == 2:
            beat(fence.info(fence.NOW + 10))
            beat(fence.info())
        return original(conn, target)
    monkeypatch.setattr(hk, '_stamp', racing)
    with pytest.raises(ValueError, match='changed while acquiring'):
        hk.capture(path, fence.NOW)
