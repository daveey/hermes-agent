"""Best-effort schema caching must not resurrect a renamed/deleted profile home."""
import json

import pytest

from hermes_constants import (
    mark_named_profile_deleted,
    reset_hermes_home_override,
    set_hermes_home_override,
)
from hermes_state_schema import SessionSchemaMixin


DDL = 'CREATE TABLE example (id INTEGER PRIMARY KEY, body TEXT NOT NULL);'
EXPECTED = {'example': {'id': 'INTEGER', 'body': 'TEXT NOT NULL'}}


@pytest.mark.parametrize('state', ['live', 'missing', 'tombstoned', 'renamed-during-parse'])
def test_schema_cache_respects_named_profile_liveness(tmp_path, monkeypatch, state):
    import hermes_state_schema as schema

    home = tmp_path / 'state'
    profile = home / 'profiles' / 'gamma'
    profile.mkdir(parents=True)
    (profile / 'config.yaml').write_text('{}')
    monkeypatch.setenv('HERMES_HOME', str(home))
    renamed = profile.with_name('delta')
    if state == 'missing':
        profile.rename(renamed)
    elif state == 'tombstoned':
        mark_named_profile_deleted(profile)
    elif state == 'renamed-during-parse':
        connect = schema.sqlite3.connect

        def rename_before_parse(*args, **kwargs):
            # The reader has resolved gamma's cache path, but the real profile move wins
            # before the reference database is parsed and the cache directory is made.
            profile.rename(renamed)
            return connect(*args, **kwargs)

        monkeypatch.setattr(schema.sqlite3, 'connect', rename_before_parse)

    token = set_hermes_home_override(str(profile))
    try:
        assert SessionSchemaMixin._parse_schema_columns(DDL) == EXPECTED
    finally:
        reset_hermes_home_override(token)

    if state == 'live':
        cache = profile / 'cache' / 'schema_columns.json'
        assert json.loads(cache.read_text())['tables'] == EXPECTED
    elif state == 'tombstoned':
        assert not (profile / 'cache').exists()
    else:
        assert not profile.exists()
        assert (renamed / 'config.yaml').is_file()
