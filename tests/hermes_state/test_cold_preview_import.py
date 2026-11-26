"""Read-only session previews must work without loading an inference runtime."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap


_COLD_PREVIEW = textwrap.dedent("""\
    import importlib.abc
    import sqlite3
    import sys

    class NoInferenceRuntime(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname in {'agent.context_compressor', 'agent.auxiliary_client'}:
                raise AssertionError(f'preview attempted inference import: {fullname}')
            return None

    sys.meta_path.insert(0, NoInferenceRuntime())
    sys.path.insert(0, sys.argv[1])
    from hermes_state_common import _shape_preview, _sql_preview_raw

    with sqlite3.connect(':memory:') as conn:
        conn.executescript('''
            CREATE TABLE sessions(id TEXT);
            CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT,
                role TEXT, content TEXT, display_kind TEXT, timestamp REAL);
            CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
            INSERT INTO sessions VALUES ('cold');
            INSERT INTO messages VALUES
                (1, 'cold', 'user', 'private scaffold', 'hidden', 1),
                (2, 'cold', 'user', 'actual user request', NULL, 2);
        ''')
        rows = conn.execute(f'SELECT s.id, {_sql_preview_raw()} FROM sessions s').fetchall()
    assert [(sid, _shape_preview(raw)) for sid, raw in rows] == [
        ('cold', 'actual user request')
    ], rows
    print('cold read-only preview passed')
""")


def test_cold_preview_does_not_require_inference_runtime(tmp_path):
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, HERMES_HOME=str(tmp_path / 'home'))
    result = subprocess.run(
        [sys.executable, '-I', '-c', _COLD_PREVIEW, str(root)],
        cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'cold read-only preview passed'
