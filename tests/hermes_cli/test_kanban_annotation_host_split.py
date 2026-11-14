"""Host header advances independently of immutable annotation bodies."""
import json

import pytest

from hermes_cli import kanban_annotation_disk as disk

SCOPE = ('split-host', 'default')


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(disk, 'FORCE', True)
    monkeypatch.setattr(disk, '_path', lambda scope: tmp_path / (scope[0] + '.json'))
    monkeypatch.setattr(disk.time, 'time', lambda: 1000.0)
    disk.reset()
    yield tmp_path
    disk.reset()


def parts(value=1):
    return ({'session': value},) + tuple({} for _ in range(11))


def header():
    return json.loads(disk._path(SCOPE).with_suffix('.host.json').read_text())


def test_identical_parts_advance_header_without_rewriting_body(world, monkeypatch):
    writes = []
    original = disk._write_json

    def record(path, body):
        writes.append(path)
        original(path, body)

    monkeypatch.setattr(disk, '_write_json', record)
    assert disk.save_host(SCOPE, 'input-1', 990, parts())
    first = header()
    assert 'parts' not in first
    assert isinstance(first['parts_digest'], str)
    writes.clear()
    assert disk.save_host(SCOPE, 'input-2', 999, parts())
    second = header()
    assert second['parts_digest'] == first['parts_digest']
    assert writes == [disk._path(SCOPE).with_suffix('.host.json')]
    disk.reset()  # another process knows only disk state
    assert disk.load_host(SCOPE) == ('input-2', 999, parts())


def test_changed_parts_get_distinct_body_old_header_remains_readable(world):
    assert disk.save_host(SCOPE, 'old', 990, parts(1))
    old = header()
    assert disk.save_host(SCOPE, 'new', 999, parts(2))
    assert header()['parts_digest'] != old['parts_digest']
    assert disk.load_host(SCOPE) == ('new', 999, parts(2))
    disk._write_json(disk._path(SCOPE).with_suffix('.host.json'), old)
    disk.reset()
    assert disk.load_host(SCOPE) == ('old', 990, parts(1))


def test_missing_body_repaired_even_with_warm_publisher(world):
    assert disk.save_host(SCOPE, 'old', 990, parts())
    bodies = list(world.glob('*.body.json'))
    assert len(bodies) == 1
    bodies[0].unlink()
    assert disk.load_host(SCOPE) is None
    assert disk.save_host(SCOPE, 'new', 999, parts())
    assert disk.load_host(SCOPE) == ('new', 999, parts())


def test_corrupt_body_is_not_adopted(world):
    assert disk.save_host(SCOPE, 'old', 990, parts())
    bodies = list(world.glob('*.body.json'))
    assert len(bodies) == 1
    bodies[0].write_text('{}')
    disk.reset()
    assert disk.load_host(SCOPE) is None


@pytest.mark.parametrize(('shared', 'warm'), [(False, False), (True, False), (True, True)],
                         ids=['cold-detached', 'cold-shared', 'warm-shared'])
def test_decodable_body_with_wrong_digest_is_not_adopted(world, shared, warm):
    assert disk.save_host(SCOPE, 'old', 990, parts(1))
    expected = ('old', 990, parts(1))
    assert disk.load_host(SCOPE, shared=shared) == expected
    original_header = header()
    body_path = disk._body_path(disk._path(SCOPE), original_header['parts_digest'])
    original_body = json.loads(body_path.read_text(encoding='utf-8'))
    changed_body = json.loads(json.dumps(original_body))
    changed_body['parts'][0]['session'] = 200
    # This must reach the digest fence, not fail JSON parsing or parts decoding.
    assert disk._decode_parts(changed_body['parts']) == parts(200)
    if not warm:
        disk.reset()
    # Replace the inode so a warm shared reader must revalidate, without timing assumptions.
    disk._write_json(body_path, changed_body)
    assert header() == original_header
    assert disk.load_host(SCOPE, shared=shared) is None
    assert disk.load_host(SCOPE, shared=shared) is None
    disk._write_json(body_path, original_body)
    assert disk.load_host(SCOPE, shared=shared) == expected


def test_body_retention_is_bounded_and_previous_is_readable(world):
    for value in range(12):
        assert disk.save_host(SCOPE, str(value), 990, parts(value))
    assert len(list(world.glob('*.body.json'))) <= 2
    assert disk.load_host(SCOPE) == ('11', 990, parts(11))


def test_warm_publisher_repairs_corrupt_body(world):
    assert disk.save_host(SCOPE, 'old', 990, parts())
    body = next(world.glob('*.body.json'))
    body.write_text('{}')
    assert disk.save_host(SCOPE, 'new', 999, parts())
    assert disk.load_host(SCOPE) == ('new', 999, parts())


def test_shared_read_reuses_body_but_observes_new_header(world, monkeypatch):
    assert disk.save_host(SCOPE, 'old', 990, parts())
    first = disk.load_host(SCOPE, shared=True)
    assert disk.save_host(SCOPE, 'new', 999, parts())
    monkeypatch.setattr(disk, '_decode_parts', lambda raw: pytest.fail('body decoded twice'))
    second = disk.load_host(SCOPE, shared=True)
    assert second[:2] == ('new', 999)
    assert second[2] is first[2]


def test_shared_read_rejects_changed_body_and_reset_clears(world, monkeypatch):
    assert disk.save_host(SCOPE, 'old', 990, parts())
    assert disk.load_host(SCOPE, shared=True) is not None
    next(world.glob('*.body.json')).write_text('{}')
    assert disk.load_host(SCOPE, shared=True) is None
    assert disk.save_host(SCOPE, 'new', 999, parts())
    assert disk.load_host(SCOPE, shared=True) is not None
    disk.reset()
    calls = []
    original = disk._decode_parts
    monkeypatch.setattr(disk, '_decode_parts', lambda raw: calls.append(True) or original(raw))
    assert disk.load_host(SCOPE, shared=True) is not None
    assert calls == [True]


def test_reader_custody_refuses_concurrent_publish(world):
    from hermes_cli.kanban_annotation_storage import custody
    assert disk.save_host(SCOPE, 'old', 990, parts())
    with custody(disk._path(SCOPE), writer=False):
        assert not disk.save_host(SCOPE, 'new', 999, parts(2))
        assert disk.load_host(SCOPE) == ('old', 990, parts())
    assert disk.save_host(SCOPE, 'new', 999, parts(2))


def test_new_publisher_reuses_existing_verified_body(world, monkeypatch):
    assert disk.save_host(SCOPE, 'old', 990, parts())
    first = disk.load_host(SCOPE, shared=True)
    disk._host_saved.clear()  # a publisher has no other process's saved-parts memo
    writes = []
    original = disk._write_json
    monkeypatch.setattr(disk, '_write_json', lambda path, body: writes.append(path) or original(path, body))
    assert disk.save_host(SCOPE, 'new', 999, parts())
    assert writes == [disk._path(SCOPE).with_suffix('.host.json')]
    second = disk.load_host(SCOPE, shared=True)
    assert second[:2] == ('new', 999)
    assert second[2] is first[2]


def test_header_publication_failure_preserves_previous_copy(world, monkeypatch):
    assert disk.save_host(SCOPE, 'old', 990, parts(1))
    original = disk._write_json

    def fail_header(path, body):
        if path.name.endswith('.host.json'):
            raise OSError('controlled header failure')
        original(path, body)

    monkeypatch.setattr(disk, '_write_json', fail_header)
    assert not disk.save_host(SCOPE, 'new', 999, parts(2))
    assert disk.load_host(SCOPE) == ('old', 990, parts(1))
