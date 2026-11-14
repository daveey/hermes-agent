"""Reject REPLACE writers that can silently invalidate the task cursor. [daveey]

This is a static literal guard, not SQL data-flow analysis: dynamically assembled
identifiers/keywords require review. The executable counterexample lives under
 tests/, outside the production roots scanned here.
"""

import ast
from pathlib import Path
import re
import sqlite3

import pytest


ROOT = Path(__file__).resolve().parents[2]
WATCHED = (
    "tasks", "task_links", "task_runs", "task_events", "task_comments",
    "task_chat_links", "kanban_notify_subs",
)
# SQL comments are legal whitespace even between INSERT / OR / REPLACE.
SPACE = r"(?:\s|/\*[\s\S]*?\*/|--[^\n]*(?:\n|$))+"
IDENT = r'''(?:[a-z_]\w*|"[^"]+"|'[^']+'|`[^`]+`|\[[^\]]+\])'''
OPTIONAL_SPACE = rf"(?:{SPACE})?"
REPLACE = re.compile(
    rf"\b(?:INSERT{SPACE}OR{SPACE})?REPLACE{SPACE}INTO{SPACE}"
    rf"(?:(?:{IDENT}){OPTIONAL_SPACE}\.{OPTIONAL_SPACE})?(?P<table>{IDENT})",
    re.IGNORECASE,
)
SOURCE_SUFFIXES = {".py", ".sql", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"}


def _violations(text):
    return [
        match.group("table")
        for match in REPLACE.finditer(text)
        if match.group("table").strip("\"'`[]").lower() in WATCHED
    ]


def _scan(root):
    failures = []
    for directory in ("hermes_cli", "gateway", "plugins"):
        for path in sorted((root / directory).rglob("*")):
            relative = path.relative_to(root)
            if (path.suffix not in SOURCE_SUFFIXES or "tests" in relative.parts
                    or "__tests__" in relative.parts or path.name.startswith("test_")
                    or any(part in {"node_modules", "__pycache__", ".git"} for part in relative.parts)):
                continue
            source = path.read_text(encoding="utf-8")
            if path.suffix == ".py":
                # AST folds adjacent literals and exposes static f-string pieces;
                # comments are excluded without an exemption for production files.
                strings = [(node.lineno, node.value) for node in ast.walk(ast.parse(source))
                           if isinstance(node, ast.Constant) and isinstance(node.value, str)]
            else:
                # Raw JS/TS strings may escape SQL identifier quotes. This is a
                # conservative source scan, not a JavaScript string evaluator.
                strings = [(1, source.replace('\\"', '"'))]
            for line, text in strings:
                for table in _violations(text):
                    failures.append(f"{relative}:{line}: REPLACE writes watched table {table}")
    return failures


def test_production_writers_do_not_replace_watched_tables():
    failures = _scan(ROOT)
    assert not failures, (
        "\n".join(failures)
        + "\nUse INSERT ... ON CONFLICT(...) DO UPDATE instead. See the writer "
        "precondition in hermes_cli/kanban_task_change.py: REPLACE can silently "
        "delete another task's row without marking its change cursor."
    )


@pytest.mark.parametrize("table", WATCHED)
@pytest.mark.parametrize("prefix", ["INSERT OR REPLACE INTO", "replace into", "INSERT\nOR\nREPLACE INTO"])
def test_every_watched_table_is_rejected(table, prefix):
    assert _violations(f"{prefix} {table} (id) VALUES (?)") == [table]


@pytest.mark.parametrize("table", ['"task_comments"', '`task_comments`', '[task_comments]', 'TASK_COMMENTS'])
def test_quoted_schema_qualified_tables(table):
    assert _violations(f"REPLACE INTO main.{table}(uid) VALUES (?)") == [table]


def test_sql_comments_between_keywords():
    assert _violations("INSERT /* why */ OR -- note\n REPLACE INTO tasks(id) VALUES (?)") == ["tasks"]


@pytest.mark.parametrize("sql", [
    "INSERT INTO tasks(id) VALUES (?) ON CONFLICT(id) DO UPDATE SET id=excluded.id",
    "INSERT OR IGNORE INTO tasks VALUES (?)",
    "INSERT OR REPLACE INTO tasks_archive VALUES (?)",
    "REPLACE INTO kanban_sync_tombstones VALUES (?)",
    "SELECT replace(body, 'x', 'y') FROM task_comments",
])
def test_safe_statements_are_allowed(sql):
    assert not _violations(sql)


@pytest.mark.parametrize("directory", ["hermes_cli", "gateway", "plugins/example"])
def test_scan_finds_scratch_caller_and_excludes_tests(tmp_path, directory):
    target = tmp_path / directory
    target.mkdir(parents=True)
    (target / "writer.py").write_text(
        'db.execute("INSERT OR REPLACE " "INTO task_comments VALUES (?)")\n'
        'db.execute(f"REPLACE INTO tasks VALUES ({value})")\n', encoding="utf-8",
    )
    (target / "test_counterexample.py").write_text(
        'db.execute("REPLACE INTO tasks VALUES (?)")', encoding="utf-8",
    )
    failures = _scan(tmp_path)
    assert len(failures) == 2
    assert all(f"{directory}/writer.py:" in failure for failure in failures)


@pytest.mark.parametrize("suffix", [".js", ".ts", ".mjs"])
@pytest.mark.parametrize("table", WATCHED)
def test_scan_escaped_quoted_identifiers(tmp_path, suffix, table):
    target = tmp_path / "plugins"
    target.mkdir()
    source = f'db.exec("REPLACE INTO \\"{table}\\" VALUES (?)")'
    (target / f"writer{suffix}").write_text(source, encoding="utf-8")
    assert len(_scan(tmp_path)) == 1


@pytest.mark.parametrize("identifier", [
    "'task_comments'",
    "main /* comment */ . task_comments",
    "main. /* comment */ task_comments",
    "'main' -- schema\n . -- table\n 'task_comments'",
])
@pytest.mark.parametrize("suffix", [".py", ".sql", ".js"])
def test_scan_sqlite_identifier_forms(tmp_path, identifier, suffix):
    sql = f"REPLACE INTO {identifier} VALUES (1, 'b')"
    # Verify these are real cross-task replacements, not invented SQL syntax.
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE task_comments (uid INTEGER UNIQUE, task TEXT)")
        db.execute("INSERT INTO task_comments VALUES (1, 'a')")
        db.execute(sql)
        assert db.execute("SELECT task FROM task_comments").fetchall() == [("b",)]
    target = tmp_path / "gateway"
    target.mkdir()
    source = f"db.execute({sql!r})" if suffix == ".py" else sql
    (target / f"writer{suffix}").write_text(source, encoding="utf-8")
    assert len(_scan(tmp_path)) == 1


def test_scan_covers_non_python_sources(tmp_path):
    target = tmp_path / "plugins"
    target.mkdir()
    (target / "writer.ts").write_text('db.exec(`REPLACE INTO tasks VALUES (?)`)', encoding="utf-8")
    assert _scan(tmp_path) == ["plugins/writer.ts:1: REPLACE writes watched table tasks"]
