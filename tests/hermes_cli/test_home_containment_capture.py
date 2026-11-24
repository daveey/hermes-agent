"""Containment intent keeps its original epoch without changing attested bytes. [daveey]"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_containment_canonical as canonical
from hermes_cli import kanban_containment_terminal_remote as terminal
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli import test_kanban_failure_transport as fixtures
from tests.hermes_cli.test_home_typed_capture import mirror

live = fixtures.live


def stage(conn, kind, task, actor):
    if kind == 'archive':
        return terminal._request(conn, 'default', 'fixture', task)[0]
    op = {'op': kind, 'task': task, 'actor_task': actor, 'actor_run_uid': sync.uuid7(),
          'client_op_id': sync.uuid7(), 'event_uid': sync.uuid7(), 'payload': {'fixture': True}}
    return canonical.stage(conn, board='default', gateway='fixture', op=op)


@pytest.mark.parametrize('kind', ['contain', 'resume_contained', 'archive'])
@pytest.mark.parametrize('epoch', [None, 'original-home-epoch'])
def test_original_custody_and_legacy_null_survive_retry(live, kind, epoch):
    task, _ = fixtures._running(live)
    actor, _ = fixtures._running(live)
    if epoch:
        mirror(live, task, epoch)
        mirror(live, actor, epoch)
    request = stage(live, kind, task, actor)
    raw = live.execute('SELECT op_json FROM containment_delivery WHERE request_uid=?', (request,)).fetchone()[0]
    assert 'home_generations' not in json.loads(raw)
    assert 'home_generations' in {r[1] for r in live.execute('PRAGMA table_info(containment_delivery)')}
    stored = live.execute('SELECT home_generations FROM containment_delivery WHERE request_uid=?', (request,)).fetchone()[0]
    expected = {task: epoch, **({actor: epoch} if kind != 'archive' else {})} if epoch else None
    assert (json.loads(stored) if stored else None) == expected
    mirror(live, task, 'new-home-epoch', pending=1)
    mirror(live, actor, 'new-home-epoch', pending=1)
    if kind == 'archive':
        assert terminal._request(live, 'default', 'fixture', task)[0] == request
    else:
        assert canonical.stage(live, board='default', gateway='fixture', op=json.loads(raw)) == request
    assert canonical.outbound(live, request, json.loads(raw)).get('home_generations') == expected
    assert live.execute('SELECT op_json FROM containment_delivery WHERE request_uid=?', (request,)).fetchone()[0] == raw
    if kind != 'archive':
        sent = []
        hub = SimpleNamespace(ident=SimpleNamespace(gateway='fixture'), ops=lambda b, ops: sent.extend(ops) or {})
        with pytest.raises(canonical.DeliveryUnknown):
            canonical.deliver(live, hub, request=request)
        assert sent == [canonical.outbound(live, request, json.loads(raw))]


@pytest.mark.parametrize('kind', ['contain', 'resume_contained', 'archive'])
@pytest.mark.parametrize('frozen_ref', ['task', 'actor'])
def test_new_containment_intent_refuses_pending_reference(live, kind, frozen_ref):
    if kind == 'archive' and frozen_ref == 'actor':
        return  # Archive has no actor-task reference.
    task, _ = fixtures._running(live)
    actor, _ = fixtures._running(live)
    mirror(live, task if frozen_ref == 'task' else actor, 'original-home-epoch', pending=1)
    canonical._schema(live)
    with pytest.raises(ValueError, match='home transfer pending'):
        stage(live, kind, task, actor)
    assert live.execute('SELECT COUNT(*) FROM containment_delivery').fetchone()[0] == 0
