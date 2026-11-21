"""Qualified cards cannot consume legacy alias receipts; keep lineage fallback live."""
from copy import deepcopy

import pytest

from hermes_cli import kanban_origin_adapter as adapter
from hermes_cli import kanban_origin_ledger as ledger
from hermes_cli import kanban_origin_legacy as legacy
from hermes_cli import kanban_session_activity as activity
from hermes_cli import profiles


@pytest.fixture
def projection(tmp_path, monkeypatch):
    import hermes_constants
    from hermes_cli import kanban_origin

    (tmp_path / 'kanban').mkdir()
    (tmp_path / 'kanban/origin-ledger.db').touch()
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    monkeypatch.setattr(adapter, '_gateway', lambda path: 'origin')
    monkeypatch.setattr(activity, '_hub_db_path', lambda: None)
    monkeypatch.setattr(activity, '_without_local_open_duplicates', lambda rows, **kw: rows)
    rows, cards, calls, resolutions = [], [], [], []
    monkeypatch.setattr(ledger, 'project', lambda *args, **kwargs: deepcopy(rows))
    monkeypatch.setattr(activity, '_legacy_remote_links', lambda: deepcopy(cards))
    original = legacy.supersedes

    def counted(card, row):
        calls.append((card.get('profile'), row['key']))
        return original(card, row)

    monkeypatch.setattr(legacy, 'supersedes', counted)

    class Connection:
        def close(self):
            pass

    monkeypatch.setattr(kanban_origin, '_ro', lambda path: Connection())

    def resolve(path, sid, conn):
        resolutions.append(sid)
        return 'tip' if sid in ('compressed', 'start', 'accepted') else sid

    monkeypatch.setattr(adapter, '_resolve_session', resolve)
    return rows, cards, calls, resolutions


def row(**changes):
    return dict(gateway='owner', origin_gateway='origin', origin_profile='default',
                origin_session='start', session='accepted', key='owner/default/main/task',
                unfinished=True, legacy_aliases=[{'key': 'main/task', 'version': 2, 'digest': 'proof'}],
                **changes)


def card(**changes):
    result = dict(gateway='owner', origin_gateway='origin', origin_profile='default',
                  session='start', key='owner/main/task', profile='default',
                  legacy_record={'key': 'main/task', 'version': 2, 'digest': 'proof'})
    result.update(changes)
    return result


@pytest.mark.parametrize('profile', ['default', '', 0, False])
def test_qualified_cards_skip_alias_scans_even_with_shared_origin(projection, profile):
    rows, cards, calls, _ = projection
    rows.extend(row() for _ in range(80))
    cards.extend(card(key=f'owner/main/other-{i}', profile=profile) for i in range(100))
    assert activity._project_remote_links() == cards + rows
    assert calls == []


@pytest.mark.parametrize('missing', [False, True])
def test_unqualified_aliases_still_superseded(projection, missing):
    rows, cards, calls, _ = projection
    rows.append(row())
    item = card(profile=None)
    if missing:
        del item['profile']
    cards.append(item)
    assert activity._project_remote_links() == rows
    assert len(calls) == 1


@pytest.mark.parametrize('field,value', [('version', 3), ('digest', 'changed'), ('key', 'other/task')])
def test_unqualified_alias_change_is_not_superseded(projection, field, value):
    rows, cards, _, _ = projection
    rows.append(row())
    item = card(profile=None)
    item['legacy_record'][field] = value
    cards.append(item)
    assert activity._project_remote_links() == cards + rows


@pytest.mark.parametrize('session', ['start', 'accepted', 'compressed'])
def test_qualified_card_retains_key_and_compression_fallback(projection, session):
    rows, cards, calls, resolutions = projection
    rows.append(row())
    cards.append(card(key=rows[0]['key'], session=session))
    assert activity._project_remote_links() == rows
    assert calls == []
    assert bool(resolutions) == (session == 'compressed')


@pytest.mark.parametrize('field', ['session', 'gateway'])
def test_qualified_card_preserves_origin_lookup_error(projection, field):
    rows, cards, _, resolutions = projection
    rows.append(row())
    cards.append(card(**{field: []}))
    with pytest.raises(TypeError, match='unhashable'):
        activity._project_remote_links()
    assert resolutions == []


def test_qualified_card_does_not_inspect_malformed_aliases(projection):
    rows, cards, calls, _ = projection
    item = row()
    item['legacy_aliases'] = 123
    rows.append(item)
    cards.append(card())
    assert activity._project_remote_links() == cards + rows
    assert calls == []


def test_profile_change_rechecks_aliases_on_next_projection(projection):
    rows, cards, calls, _ = projection
    rows.append(row())
    cards.append(card())
    assert activity._project_remote_links() == cards + rows
    cards[0]['profile'] = None
    assert activity._project_remote_links() == rows
    assert calls == [(None, rows[0]['key'])]
