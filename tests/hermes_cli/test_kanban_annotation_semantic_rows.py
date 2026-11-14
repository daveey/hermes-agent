"""Incremental task-token inventory is separate from the strict replica cache."""
import json

import pytest

from hermes_cli import kanban_annotation_semantic as semantic
from hermes_cli.kanban_replica_rows import Rows, pack
from tests.hermes_cli.test_kanban_annotation_semantic import signed


def view(gen, tasks, *, base=None, changed=(), removed=(), ident=(1, 2)):
    rows = {key: (*key, pack(json.dumps(task)), gen * 1000, gen) for key, task in tasks.items()}
    return Rows(path="replica", ident=ident, gen=gen, base_gen=base,
        changed=frozenset(changed), removed=frozenset(removed), by_key=rows,
        version_column="version", meta={}, sums=(), last_seq=gen)


def test_delta_liveness_and_substantive(monkeypatch):
    key = ("owner", "default/default/t_one")
    index = semantic.TaskTokens()
    first = index.advance(view(1, {key: signed()}))
    calls = []
    original = semantic.task_token
    monkeypatch.setattr(semantic, "task_token", lambda task: (calls.append(task), original(task))[1])
    second = index.advance(view(2, {key: signed(activity_at=2)}, base=1, changed=[key]))
    assert second == first and len(calls) == 1
    third = index.advance(view(3, {key: signed(title="changed")}, base=2, changed=[key]))
    assert third != second
    assert first == second  # previously returned inventory never mutates


def test_same_view_no_decode(monkeypatch):
    key = ("owner", "default/default/t_one")
    index = semantic.TaskTokens()
    current = view(1, {key: signed()})
    expected = index.advance(current)
    monkeypatch.setattr(semantic, "task_token", lambda _: pytest.fail("unchanged decoded"))
    assert index.advance(current) is expected


def test_skipped_generation_and_deletion():
    a, b = ("owner", "default/default/t_one"), ("owner", "default/default/t_two")
    index = semantic.TaskTokens()
    old = index.advance(view(1, {a: signed(), b: signed(id="t_two")}))
    new = index.advance(view(3, {a: signed(title="new")}, base=2, changed=[]))
    assert b not in new and old[a] != new[a]


def test_delta_removal():
    key = ("owner", "default/default/t_one")
    index = semantic.TaskTokens()
    index.advance(view(1, {key: signed()}))
    assert not index.advance(view(2, {}, base=1, removed=[key]))


def test_identity_change_with_reused_generation():
    key = ("owner", "default/default/t_one")
    index = semantic.TaskTokens()
    before = index.advance(view(1, {key: signed()}))
    after = index.advance(view(1, {key: signed(title="restore")}, ident=(1, 3)))
    assert before != after


def test_malformed_is_not_committed_and_recovers():
    key = ("owner", "default/default/t_one")
    index = semantic.TaskTokens()
    before = index.advance(view(1, {key: signed()}))
    bad = view(2, {key: signed()}, base=1, changed=[key])
    bad.by_key[key] = (*key, pack("not json"), 2, 2)
    with pytest.raises(ValueError):
        index.advance(bad)
    after = index.advance(view(3, {key: signed(title="recover")}, base=2))
    assert before != after


def test_inventory_read_only():
    key = ("owner", "default/default/t_one")
    result = semantic.TaskTokens().advance(view(1, {key: signed()}))
    with pytest.raises(TypeError):
        result[key] = "corruption"


def test_missing_identity_does_not_reuse_generation():
    key = ("owner", "default/default/t_one")
    index = semantic.TaskTokens()
    before = index.advance(view(1, {key: signed()}, ident=None))
    after = index.advance(view(1, {key: signed(title="changed")}, ident=None))
    assert before != after


@pytest.mark.parametrize("key,task", [
    (("owner", "default/default/wrong"), signed()),
    (("owner", "invalid"), signed()),
    (("owner", "default//t_one"), signed()),
])
def test_record_identity_must_match(key, task):
    with pytest.raises(ValueError, match="identity"):
        semantic.TaskTokens().advance(view(1, {key: task}))


def test_full_equal_inventory_no_decode(monkeypatch):
    key = ("owner", "default/default/t_one")
    index = semantic.TaskTokens()
    expected = index.advance(view(1, {key: signed()}))
    monkeypatch.setattr(semantic, "task_token", lambda _: pytest.fail("unchanged decoded"))
    assert index.advance(view(5, {key: signed()})) == expected


@pytest.mark.parametrize("data,error", [
    ('{"id":"t_one","extra":NaN}', ValueError),
    ('{"id":"t_one","extra":"\\ud800","origin_observation":{"schema":1}}', UnicodeError),
])
def test_unkeyable_row_keeps_old_generation(data, error):
    key = ("owner", "default/default/t_one")
    index = semantic.TaskTokens()
    good = view(1, {key: signed()})
    expected = index.advance(good)
    bad = view(2, {key: signed()}, base=1, changed=[key])
    bad.by_key[key] = (*key, pack(data), 2, 2)
    with pytest.raises(error):
        index.advance(bad)
    assert index.advance(good) is expected

