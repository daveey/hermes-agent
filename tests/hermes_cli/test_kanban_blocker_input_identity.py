"""Unknown identity and physical/schema changes cannot reuse blocker inputs."""
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_blocker_inputs as inputs
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_shared_blockers as helpers

kanban_home = helpers.kanban_home


@pytest.mark.parametrize('change', ['schema', 'main_file'])
def test_schema_and_file_identity_invalidate(kanban_home, change):
    with kbc.connect_closing() as conn:
        calls = []

        def compute():
            calls.append(1)
            return len(calls)

        @inputs.input_pass
        def exercise():
            assert inputs.read('v', conn, 10, compute) == 1
            if change == 'schema':
                conn.execute('CREATE TABLE schema_probe (value INTEGER)')
            else:
                path = Path(conn.execute('PRAGMA database_list').fetchone()[2])
                st = path.stat()
                os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1000000000))
            assert inputs.read('v', conn, 10, compute) == 2
        exercise()


def test_unreadable_key_falls_back(kanban_home, monkeypatch):
    from hermes_cli import config
    from hermes_cli.config_backend import ConfigBackendUnavailable
    with kbc.connect_closing() as conn:
        calls = []

        def unavailable():
            raise ConfigBackendUnavailable('unavailable')

        def compute():
            calls.append(1)
            return len(calls)

        @inputs.input_pass
        def exercise():
            assert inputs.read('v', conn, 10, compute) == 1
            monkeypatch.setattr(config, 'load_config_readonly', unavailable)
            assert inputs.read('v', conn, 10, compute) == 2
            assert inputs.read('v', conn, 10, compute) == 3
        exercise()
