"""Display adoption is bounded and code-matched, never an exact-input proof."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_annotation_mutation as mutation
from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_kanban_annotation_host as helpers

world = helpers.world
SCOPE = helpers.SCOPE


def test_display_adopts_moved_inputs_without_probing(world, monkeypatch):
    helpers._poll()
    copy = host._disk.load_host(SCOPE)
    helpers._other_process()
    helpers._advance(world, 2)
    world['conn'].execute("UPDATE tasks SET status='done' WHERE id='t1'")
    monkeypatch.setattr(host, '_inputs_now', lambda *a: pytest.fail('display exact probe'))
    parts = host.adopt_display(SCOPE)
    assert parts == copy[2]
    assert ksa.annotation_read_at(parts) == copy[1]
    assert world['calls']['recompute'] == 1


@pytest.mark.parametrize('age,accepted', [(12, True), (12.001, False), (-1, False)])
def test_display_age_bound(world, age, accepted):
    helpers._poll()
    helpers._advance(world, age)
    assert (host.adopt_display(SCOPE) is not None) is accepted


def test_display_code_and_registration_identity(world):
    helpers._poll()
    world['sha'][0] = 'b' * 40
    assert host.adopt_display(SCOPE) is None
    world['sha'][0] = 'a' * 40
    dispose = ksa.register_done_check('new-check', lambda card: False)
    try:
        assert host.adopt_display(SCOPE) is None
    finally:
        dispose()
    assert host.adopt_display(SCOPE) is not None


def test_display_pending_own_cue_rejected(world):
    helpers._poll()
    mutation.record(SCOPE)
    assert host.adopt_display(SCOPE) is None


def test_strict_adoption_still_rejects_moved_inputs(world):
    helpers._poll()
    helpers._advance(world, 2)
    world['conn'].execute("UPDATE tasks SET status='done' WHERE id='t1'")
    assert host.adopt(SCOPE) is None
    assert host.adopt_display(SCOPE) is not None


def test_display_reruns_undeclared_waiting_source(world):
    waiting = {'s3': ['t9']}
    ksa.register_waiting_source('undeclared', lambda: waiting)
    try:
        helpers._poll()
        waiting.clear()
        assert host.adopt_display(SCOPE)[9] == {}
    finally:
        ksa.register_waiting_source('undeclared', None)


def test_display_rejects_old_body_even_if_new_header_acknowledges_cue(world, monkeypatch):
    helpers._poll()
    load = host._disk.load_host

    def racing_load(*args, **kwargs):
        copy = load(*args, **kwargs)
        mutation.record(SCOPE)
        ksa._revalidate.force_read.scope = SCOPE
        try:
            ksa._refresh_annotation_locked(SCOPE)
        finally:
            ksa._revalidate.force_read.scope = None
        return copy

    monkeypatch.setattr(host._disk, 'load_host', racing_load)
    assert host.adopt_display(SCOPE) is None


def test_display_snapshot_bypasses_exact_revalidation(world, monkeypatch):
    helpers._poll()
    helpers._advance(world, 2)
    world['conn'].execute("UPDATE tasks SET status='done' WHERE id='t1'")
    monkeypatch.setattr(ksa._revalidate, 'refresh_if_unchanged', lambda *a: pytest.fail('expensive exact probe'))
    assert ksa._annotation_snapshot(display=True) is not None


def test_strict_publication_survives_unavailable_display_identity(world, monkeypatch):
    def unavailable(*args):
        raise ValueError('unknown code')
    monkeypatch.setattr(host, '_display_identity', unavailable)
    helpers._poll()
    assert host._disk.load_host(SCOPE) is not None
    assert host.adopt(SCOPE) is not None
    assert host.adopt_display(SCOPE) is None


def test_mixed_annotation_block_does_not_share_display_with_strict(world, monkeypatch):
    helpers._poll()
    calls = []
    original = ksa._annotation_snapshot
    def snapshot(*, display=False):
        calls.append(display)
        return original(display=display)
    monkeypatch.setattr(ksa, '_annotation_snapshot', snapshot)
    with ksa.one_annotation_snapshot():
        ksa.annotate_kanban_activity([{'id': 's1'}], display=True)
        ksa.annotate_kanban_activity([{'id': 's1'}])
    assert calls == [True, False]
