"""A normalized COUNTS receipt cannot revoke independent completed readers."""
import pytest

from hermes_cli import kanban_annotation_counts as counts
from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_annotation_parts as parts
from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_annotation_heartbeat_fence as fence
from tests.hermes_cli import test_kanban_annotation_waiting_gate as gate

world = gate.world
waiting = gate.waiting
replica = fence.replica


def test_session_commit_during_counts_preserves_waiting_certificate(waiting, replica, monkeypatch):
    path, writer, _beat = replica
    monkeypatch.setattr(ksa, '_hub_db_path', lambda: path)
    monkeypatch.setattr(fence.hk.time, 'time', lambda: fence.NOW)
    original = ksa._recompute_all_counts
    reads = []

    def crossing(*args, **kwargs):
        value = original(*args, **kwargs)
        reads.append(True)
        writer.execute('INSERT INTO sessions VALUES(?)', (len(reads),))
        return value

    monkeypatch.setattr(ksa, '_recompute_all_counts', crossing)
    gate.refresh()
    assert waiting.reads == 1
    gate.refresh()
    assert len(reads) == 2, 'the rejected COUNTS receipt must force another actual count read'
    assert waiting.reads == 1, 'a session-only commit revoked an independent waiting answer'
    assert parts.COUNTS not in parts._seen[fence.holders.SCOPE]
    assert fence.holders.SCOPE not in counts._entries
    assert ksa._last_annotation[fence.holders.SCOPE][9] == {'s1': frozenset({'t1'})}
    waiting.key += 1
    waiting.ids = {'changed'}
    gate.refresh()
    assert waiting.reads == 2
    assert ksa._last_annotation[fence.holders.SCOPE][9] == {'s1': frozenset({'changed'})}


@pytest.mark.parametrize('moved', [parts.LINKS, parts.ATTENTION, parts.WAITING])
def test_rejected_receipt_keeps_original_keys_not_later_inputs(world, replica, monkeypatch, moved):
    path, writer, _beat = replica
    _semantic, receipt = fence.hk.capture(path, fence.NOW)
    original = parts._Keys({part: object() for part in parts.ALL})
    original.receipt = receipt
    old = dict(original)
    scope = fence.holders.SCOPE
    parts._waiting[scope] = (('s1', frozenset({'original'})),)
    counts._entries[scope] = counts.Source(counts.CountSnapshot((), {}), old[parts.COUNTS], 0.0, 0.0, (), None)
    writer.execute('INSERT INTO sessions VALUES(1)')
    assert not receipt.valid()
    parts.done(scope, original)
    assert dict(original) == old, 'done mutated the plan held by counts.begin'
    assert parts._seen[scope] == {part: key for part, key in old.items() if part != parts.COUNTS}
    assert scope not in counts._entries
    assert parts._waiting[scope] == (('s1', frozenset({'original'})),)
    later = dict(original)
    later[moved] = object()
    monkeypatch.setattr(parts, 'keys', lambda _scope: later)
    forced, _keys = parts.plan(scope, clock=False)
    assert forced == {parts.COUNTS, moved}
    assert parts.plan(scope, clock=True)[0] == parts.ALL
    parts.failed(scope)
    assert scope not in parts._seen and scope not in parts._waiting
    assert parts.plan(scope, clock=False)[0] == parts.ALL


def test_receipt_error_still_invalidates_entire_holder(waiting, replica, monkeypatch):
    path, writer, _beat = replica
    monkeypatch.setattr(ksa, '_hub_db_path', lambda: path)
    monkeypatch.setattr(fence.hk.time, 'time', lambda: fence.NOW)
    original = ksa._recompute_all_counts

    def crossing(*args, **kwargs):
        value = original(*args, **kwargs)
        writer.execute('INSERT INTO sessions VALUES(1)')
        return value

    monkeypatch.setattr(ksa, '_recompute_all_counts', crossing)
    gate.refresh()
    scope = fence.holders.SCOPE
    assert scope in parts._seen and scope in parts._waiting

    def broken(_receipt):
        raise ValueError('unavailable publication fence')

    monkeypatch.setattr(fence.hk.Receipt, 'valid', broken)
    gate.refresh()
    assert scope not in parts._seen and scope not in parts._waiting
    assert scope not in counts._entries


def test_missing_or_valid_receipt_preserves_all_certificates(world, replica):
    path, _writer, _beat = replica
    _key, receipt = fence.hk.capture(path, fence.NOW)
    scope = fence.holders.SCOPE
    for carrier in (dict, parts._Keys):
        keys = carrier({part: object() for part in parts.ALL})
        if isinstance(keys, parts._Keys):
            keys.receipt = receipt
        parts.done(scope, keys)
        assert parts._seen[scope] == keys


def test_late_outer_failure_clears_independent_output(waiting, replica, monkeypatch):
    path, writer, _beat = replica
    monkeypatch.setattr(ksa, '_hub_db_path', lambda: path)
    monkeypatch.setattr(fence.hk.time, 'time', lambda: fence.NOW)
    original = ksa._recompute_all_counts

    def crossing(*args, **kwargs):
        value = original(*args, **kwargs)
        writer.execute('INSERT INTO sessions VALUES(1)')
        return value

    monkeypatch.setattr(ksa, '_recompute_all_counts', crossing)
    gate.refresh()
    scope = fence.holders.SCOPE
    assert scope in parts._seen and scope in parts._waiting
    publish = host.publish

    def broken(*args, **kwargs):
        raise ValueError('publication refused after independent readers')

    monkeypatch.setattr(host, 'publish', broken)
    waiting.key += 1
    waiting.ids = {'unpublished'}
    gate.refresh()
    assert scope not in parts._seen and scope not in parts._waiting
    assert scope not in counts._entries
    monkeypatch.setattr(host, 'publish', publish)
    gate.refresh()
    assert waiting.reads == 3
    assert ksa._last_annotation[scope][9] == {'s1': frozenset({'unpublished'})}
