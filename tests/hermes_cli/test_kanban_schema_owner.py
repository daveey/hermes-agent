"""Only a home's installed core changes its boards' schema; a SchemaAhead give-up opens one hub card. [daveey]

Incident (2026-11-12): a checkout of origin/main (95fed77843) launched against David-MBP's live ``~/.hermes`` at
23:53:41 PST (daveey-mini: 23:58:38) while the installed core was older. Its board connect added
``tasks.review_required``, ``task_events.origin_gateway`` and the ``admission_*`` columns; three seconds later
the gateway found columns its core does not declare (``SchemaAhead``) and gave up the Board sync role for hours.
Nothing opened a card. Cases (card t_01a1d3fb5bab541f):

(a) a non-installed checkout leaves an older board's ``PRAGMA table_info`` unchanged and refuses writes with a
    line naming the installed core and a scratch home;
(b) the installed core (or a tree at its commit) migrates as before;
(c) boot home maintenance from a non-installed checkout skips board migrations;
(d) a SchemaAhead give-up opens one hub card per host incident and comments on it while it is open.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_schema_owner as owner
from hermes_cli import kanban_sync_schema_alert as alert
from hermes_cli import post_update
from tests.hermes_cli import kanban_sync_support as support

DROPPED = ("tasks", "review_required")


@pytest.fixture
def home(tmp_path, monkeypatch):
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_SESSION_ID", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(var, raising=False)
    h = support.kanban_home(tmp_path, monkeypatch)
    monkeypatch.setattr(owner, "_DECISIONS", {})
    monkeypatch.setattr(owner, "READ_ONLY", {})
    yield h
    for path in (h / "kanban.db",):
        kbc._INITIALIZED_PATHS.discard(str(path.resolve()))


def _older_board(home: Path) -> Path:
    """A board made by an older core: the current schema minus ``tasks.review_required``."""
    db = home / "kanban.db"
    conn = kbc.connect(db)
    kb.create_task(conn, title="older card", assignee="default")
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall():
        conn.execute(f'DROP TRIGGER "{name}"')  # the older core's own init recreates its triggers
    conn.execute(f"ALTER TABLE {DROPPED[0]} DROP COLUMN {DROPPED[1]}")
    conn.close()
    kbc._INITIALIZED_PATHS.discard(str(db.resolve()))
    return db


def _columns(db: Path, table: str = "tasks") -> list[str]:
    raw = sqlite3.connect(db)
    try:
        return [r[1] for r in raw.execute(f"PRAGMA table_info({table})")]
    finally:
        raw.close()


def _triggers(db: Path) -> list[tuple]:
    raw = sqlite3.connect(db)
    try:
        return raw.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name").fetchall()
    finally:
        raw.close()


def _foreign_install(home: Path) -> Path:
    """``<home>/hermes-agent``: an installed core that is not the tree these tests run from."""
    core = home / "hermes-agent"
    (core / "hermes_cli").mkdir(parents=True)
    (core / "install-stamp.json").write_text(json.dumps({"commit": "2cc7101849" + "0" * 30}))
    return core


# --- (a) a non-installed checkout ---------------------------------------------------------------------------

def test_non_installed_checkout_leaves_the_schema_and_refuses_writes(home):
    db = _older_board(home)
    before = _columns(db)
    core = _foreign_install(home)

    conn = kbc.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1  # reads work
        with pytest.raises(owner.BoardSchemaNotOwned) as err:
            kb.create_task(conn, title="from a worktree", assignee="default")
    finally:
        conn.close()
    assert _columns(db) == before
    assert DROPPED[1] not in _columns(db)
    line = str(err.value)
    assert str(core.resolve()) in line and "2cc7101849" in line
    assert "HERMES_HOME=$(mktemp -d)" in line and "review_required" in line
    assert "\n" not in line
    # The same process keeps the board read-only on later connects.
    again = kbc.connect(db)
    try:
        with pytest.raises(owner.BoardSchemaNotOwned):
            again.execute("UPDATE tasks SET title = 'x'")
    finally:
        again.close()
    assert _columns(db) == before


@pytest.mark.parametrize("missing_counter", [False, True])
def test_non_installed_checkout_opens_a_current_board_normally(home, missing_counter):
    from hermes_cli import kanban_notify_revision as notify_rev

    db = home / "kanban.db"
    kbc.connect(db).close()  # created by "the installed core" (no install yet)
    if missing_counter:
        with sqlite3.connect(db) as raw:
            for name in notify_rev.TRIGGERS:
                raw.execute(f'DROP TRIGGER "{name}"')
            raw.execute(f'DROP TABLE "{notify_rev.TABLE}"')
    before = _triggers(db)
    kbc._INITIALIZED_PATHS.discard(str(db.resolve()))
    _foreign_install(home)
    conn = kbc.connect(db)
    try:
        tid = kb.create_task(conn, title="compatible board", assignee="default")
        assert conn.execute("SELECT id FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert not owner.missing_schema(conn)
        if missing_counter:
            assert notify_rev.read_revision(conn, ()) is None
    finally:
        conn.close()
    assert _triggers(db) == before


def test_reference_schema_matches_what_connect_creates(home):
    """The "at par" reference is every table/column connect()'s schema pass creates: no step is left out.
    The display-only activity/change and notification revision tables are outside it: their absence
    leaves readers on a stat fallback or a full read."""
    from hermes_cli.kanban_activity_revision import CHANGE_TABLE, REVISION_TABLE
    from hermes_cli.kanban_notify_revision import TABLE as NOTIFY_REVISION_TABLE
    from hermes_cli.kanban_annotation_revision import KINDS
    optional = {REVISION_TABLE, CHANGE_TABLE, NOTIFY_REVISION_TABLE, *(k.table for k in KINDS)}
    db = home / "kanban.db"
    kbc.connect(db).close()
    raw = sqlite3.connect(db)
    try:
        created = {t: c for t, c in owner._tables(raw).items() if t not in optional}
        assert created == owner._reference_schema()
    finally:
        raw.close()


def test_board_missing_a_table_the_installed_core_predates_is_not_at_par(home):
    """A board without ``kanban_source_receipt_pending`` (installed core predates it) stays read-only here
    with the clear refusal, not a read-write open that fails later with a raw "no such table"."""
    from hermes_cli import kanban_source_receipt
    db = _older_board(home)
    raw = sqlite3.connect(db)
    raw.execute(f'DROP TABLE "{kanban_source_receipt.TABLE}"')
    raw.execute("ALTER TABLE tasks ADD COLUMN review_required INTEGER NOT NULL DEFAULT 0")  # other columns at par
    raw.commit()
    raw.close()
    before = _triggers(db), _columns(db)
    _foreign_install(home)
    probe = sqlite3.connect(db)
    try:
        assert kanban_source_receipt.TABLE in owner.missing_schema(probe)
    finally:
        probe.close()
    conn = kbc.connect(db)
    try:
        with pytest.raises(owner.BoardSchemaNotOwned) as err:
            kb.create_task(conn, title="from a worktree", assignee="default")
    finally:
        conn.close()
    assert kanban_source_receipt.TABLE in str(err.value)
    assert (_triggers(db), _columns(db)) == before


# --- (b) the installed core ---------------------------------------------------------------------------------

def test_installed_core_migrates_as_before(home):
    db = _older_board(home)
    (home / "hermes-agent").symlink_to(owner.running_core(), target_is_directory=True)
    conn = kbc.connect(db)
    try:
        kb.create_task(conn, title="installed core writes", assignee="default")
    finally:
        conn.close()
    assert DROPPED[1] in _columns(db)


def test_a_tree_at_the_installed_commit_migrates(home, monkeypatch):
    db = _older_board(home)
    _foreign_install(home)
    monkeypatch.setattr(owner, "_identity", lambda root: "a" * 40)
    kbc.connect(db).close()
    assert DROPPED[1] in _columns(db)


def test_home_without_an_installed_core_migrates(home):
    db = _older_board(home)
    kbc.connect(db).close()
    assert DROPPED[1] in _columns(db)


# --- (c) boot home maintenance ------------------------------------------------------------------------------

def test_boot_home_maintenance_from_a_non_installed_checkout_skips_board_migration(home, caplog):
    db = _older_board(home)
    before = _columns(db)
    _foreign_install(home)
    steps = [step for step in post_update.BOOT_HOME_STEPS if step[0] == "kanban_board_schema"]
    assert steps, "boot home maintenance has no board-schema step"
    with caplog.at_level("INFO", logger=post_update.logger.name):
        results = post_update.run_steps(steps)
    assert results["kanban_board_schema"]["skipped"] == "not-installed-core"
    assert results["kanban_board_schema"]["installed"] == str((home / "hermes-agent").resolve())
    assert any("skipping board migrations" in r.getMessage() for r in caplog.records)
    assert _columns(db) == before


def test_boot_home_maintenance_of_the_installed_core_leaves_migration_to_first_connect(home):
    (home / "hermes-agent").symlink_to(owner.running_core(), target_is_directory=True)
    assert post_update.step_kanban_board_schema()["skipped"] == "first-connect"


# --- (d) the hub card ---------------------------------------------------------------------------------------

class FakeHubCli:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.open: list[dict] = []

    def __call__(self, argv: list[str]):
        self.calls.append(argv)
        if argv[1] == "tasks":
            return 0, json.dumps(self.open), ""
        return 0, "queued", ""

    def sent(self, verb: str) -> list[list[str]]:
        return [c for c in self.calls if c[1] == verb]


INFO = {"installed": "/Users/daveey/.hermes/hermes-agent", "installed_sha": "2cc7101849", "newest_sha": "95fed77843",
        "origin_main_sha": "95fed77843"}
WHY = "board default: SchemaAhead: this core does not declare columns a newer core added (tasks: review_required)"


def test_schema_ahead_give_up_opens_one_card_per_incident_and_comments_while_open(home):
    hub = FakeHubCli()

    rid = alert.report("macbook", WHY, 900.0, runner=hub, hub_cli="hermes-hub", info=INFO, now=1_000.0)
    creates = hub.sent("create-task")
    assert len(creates) == 1 and rid == "kanban-sync-schema-ahead:macbook:900"
    argv = creates[0]
    assert argv[2] == "hub" and "macbook" in argv[3]
    body = argv[argv.index("--body") + 1]
    assert "review_required" in body and "2cc7101849" in body and "95fed77843" in body
    assert 'idempotency_key="kanban-sync-schema-ahead:macbook:900"' in argv

    # The same incident again (a respun refusal): nothing new.
    assert alert.report("macbook", WHY, 900.0, runner=hub, hub_cli="hermes-hub", info=INFO, now=1_000.0) is None
    assert len(hub.sent("create-task")) == 1 and not hub.sent("request")

    # A new incident while the card is open: a comment on it, not a second card.
    hub.open = [{"id": "t_open", "status": "ready", "idempotency_key": "kanban-sync-schema-ahead:macbook:900"}]
    assert alert.report("macbook", WHY, 2_000.0, runner=hub, hub_cli="hermes-hub", info=INFO, now=1_000.0) == "kanban-sync-schema-ahead:macbook:2000:comment"
    assert len(hub.sent("create-task")) == 1
    (comment,) = hub.sent("request")
    assert comment[2:6] == ["hub", "tasks", "default/default/t_open", "comment"]
    assert "review_required" in comment[comment.index("--arg") + 1]

    # Once that card is done, the next incident opens a new card.
    hub.open = []
    alert.report("macbook", WHY, 3_000.0, runner=hub, hub_cli="hermes-hub", info=INFO, now=1_000.0 + 7 * 3600)
    assert len(hub.sent("create-task")) == 2


def test_another_hosts_open_card_does_not_absorb_this_hosts_incident(home):
    hub = FakeHubCli()
    hub.open = [{"id": "t_mini", "status": "ready", "idempotency_key": "kanban-sync-schema-ahead:mini:5"}]
    alert.report("macbook", WHY, 900.0, runner=hub, hub_cli="hermes-hub", info=INFO, now=1_000.0)
    assert len(hub.sent("create-task")) == 1 and not hub.sent("request")


def test_a_card_filed_and_closed_within_the_fresh_window_gets_a_new_card_not_a_comment(home):
    hub = FakeHubCli()
    alert.report("macbook", WHY, 900.0, runner=hub, hub_cli="hermes-hub", info=INFO, now=1_000.0)
    state = alert._read_state()
    alert._write_state({**state, "task_id": "t_first"})
    hub.open = [{"id": "t_first", "status": "done", "idempotency_key": "kanban-sync-schema-ahead:macbook:900"}]
    alert.report("macbook", WHY, 1_100.0, runner=hub, hub_cli="hermes-hub", info=INFO, now=1_200.0)
    assert len(hub.sent("create-task")) == 2 and not hub.sent("request")
    assert all("--all" in c for c in hub.sent("tasks"))


# --- the review finding: a non-installed core at par keeps the board's sync mode ------------------------

def test_non_installed_checkout_on_a_synced_board_at_par_keeps_its_sync_mode(home):
    from hermes_cli import kanban_db_sync as sync
    support.write_sync_config(home, True)
    db = home / "kanban.db"
    conn = kbc.connect(db)  # "the installed core" (no install yet) creates it and installs sync
    assert sync.board_mode(conn) == "shadow"
    conn.close()
    resolved = str(db.resolve())
    kbc._INITIALIZED_PATHS.discard(resolved)
    sync._SYNCED_PATHS.discard(resolved)
    _foreign_install(home)
    triggers_before = _triggers(db)
    assert triggers_before
    conn = kbc.connect(db)
    try:
        assert sync.board_mode(conn) == "shadow"
        tid = kb.create_task(conn, title="board id, not a classic one", assignee="default")
        assert not (len(tid) == 10 and tid.startswith("t_"))  # classic ids are t_ + 8 hex
    finally:
        conn.close()
    assert _triggers(db) == triggers_before


# --- follow-ups from review 5889519484 (card t_01a1d47a6e38a25b) -------------------------------------------

def _synced_board(home: Path) -> Path:
    """A board the installed core created with sync on (capture triggers, ``shadow`` mode)."""
    support.write_sync_config(home, True)
    db = home / "kanban.db"
    kbc.connect(db).close()
    resolved = str(db.resolve())
    kbc._INITIALIZED_PATHS.discard(resolved)
    from hermes_cli import kanban_db_sync as sync
    sync._SYNCED_PATHS.discard(resolved)
    return db


def test_reference_schema_with_sync_matches_what_a_synced_connect_creates(home, monkeypatch):
    """With sync live, the at-par reference also holds every sync table/column connect() installs."""
    from hermes_cli.kanban_activity_revision import CHANGE_TABLE, REVISION_TABLE
    from hermes_cli.kanban_notify_revision import TABLE as NOTIFY_REVISION_TABLE
    from hermes_cli.kanban_annotation_revision import KINDS
    optional = {REVISION_TABLE, CHANGE_TABLE, NOTIFY_REVISION_TABLE, *(k.table for k in KINDS)}
    monkeypatch.setattr(owner, "_REFERENCE", {})
    db = _synced_board(home)
    raw = sqlite3.connect(db)
    try:
        created = {t: c for t, c in owner._tables(raw).items() if t not in optional}
        assert "kanban_sync_state" in created
        assert created == owner._reference_schema(sync=True)
    finally:
        raw.close()


def test_synced_board_missing_a_sync_column_this_core_declares_is_not_at_par(home, monkeypatch):
    """Main adds a sync column the installed core lacks: a non-installed checkout must refuse with
    BoardSchemaNotOwned, not open read-write with sync live and fail later on a raw "no such column"."""
    from hermes_cli import kanban_db_sync as sync
    db = _synced_board(home)
    before = _triggers(db), _columns(db)
    monkeypatch.setattr(owner, "_REFERENCE", {})
    monkeypatch.setattr(sync, "_SYNC_COLUMN_DDL", (*sync._SYNC_COLUMN_DDL, ("tasks", "fake_sync_col", "fake_sync_col TEXT")))
    _foreign_install(home)
    conn = kbc.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0  # reads still work
        with pytest.raises(owner.BoardSchemaNotOwned) as err:
            kb.create_task(conn, title="from a worktree", assignee="default")
    finally:
        conn.close()
    assert "fake_sync_col" in str(err.value)
    assert (_triggers(db), _columns(db)) == before


def test_unsynced_board_ignores_sync_ddl(home, monkeypatch):
    """Sync off for the board: the sync DDL is not part of "at par" (nothing would touch those columns)."""
    from hermes_cli import kanban_db_sync as sync
    db = home / "kanban.db"
    kbc.connect(db).close()
    kbc._INITIALIZED_PATHS.discard(str(db.resolve()))
    monkeypatch.setattr(owner, "_REFERENCE", {})
    monkeypatch.setattr(sync, "_SYNC_COLUMN_DDL", (*sync._SYNC_COLUMN_DDL, ("tasks", "fake_sync_col", "fake_sync_col TEXT")))
    _foreign_install(home)
    conn = kbc.connect(db)
    try:
        kb.create_task(conn, title="sync off, at par", assignee="default")
    finally:
        conn.close()


def test_non_installed_checkout_does_not_create_a_missing_board(home):
    """A board file that does not exist yet stays absent: no 4096-byte empty kanban.db, no sidecars."""
    db = home / "kanban.db"
    assert not db.exists()
    core = _foreign_install(home)
    with pytest.raises(owner.BoardSchemaNotOwned) as err:
        kbc.connect(db)
    assert not db.exists()
    assert not any(p.name.startswith("kanban.db") for p in home.iterdir()), sorted(p.name for p in home.iterdir())
    line = str(err.value)
    assert str(core.resolve()) in line and "a new board" in line and "\n" not in line


def test_cli_prints_the_refusal_line_not_a_traceback(home, capsys):
    import argparse
    from hermes_cli import kanban as kcli
    from hermes_cli.kanban_parser import build_parser
    db = _older_board(home)
    _foreign_install(home)
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    rc = kcli.kanban_command(parser.parse_args(["kanban", "create", "from a worktree", "--assignee", "default"]))
    err = capsys.readouterr().err
    assert rc == 1
    assert "Traceback" not in err
    lines = [line for line in err.splitlines() if line.strip()]
    assert len(lines) == 1 and lines[0].startswith("kanban: ") and "only this home's installed core" in lines[0]
    assert not lines[0].startswith("kanban: kanban:")
    assert DROPPED[1] not in _columns(db)


# --- mutant survivors of #2207 (card t_01a1d509cb5adc68) ---------------------------------------------------

def _fake_sync_column(monkeypatch) -> None:
    from hermes_cli import kanban_db_sync as sync
    monkeypatch.setattr(owner, "_REFERENCE", {})
    monkeypatch.setattr(sync, "_SYNC_COLUMN_DDL", (*sync._SYNC_COLUMN_DDL, ("tasks", "fake_sync_col", "fake_sync_col TEXT")))


def test_unsynced_reference_built_first_does_not_stand_in_for_the_synced_one(home, monkeypatch):
    """One process judges both modes: the unsynced reference, built first, must not be reused for a synced
    board, or a sync column this core declares and the board lacks would pass as "at par"."""
    db = _synced_board(home)
    before = _triggers(db), _columns(db)
    _fake_sync_column(monkeypatch)
    unsynced = owner._reference_schema(sync=False)
    assert "kanban_sync_state" not in unsynced
    synced = owner._reference_schema(sync=True)
    assert "kanban_sync_state" in synced and "fake_sync_col" in synced["tasks"]
    _foreign_install(home)
    conn = kbc.connect(db)
    try:
        with pytest.raises(owner.BoardSchemaNotOwned) as err:
            kb.create_task(conn, title="from a worktree", assignee="default")
    finally:
        conn.close()
    assert "fake_sync_col" in str(err.value)
    assert (_triggers(db), _columns(db)) == before


def test_synced_reference_built_first_is_not_applied_to_an_unsynced_board(home, monkeypatch):
    """The reverse order: the synced reference, built first, must not judge an unsynced board (it has no sync
    tables and needs none), which would turn a board at par into a read-only refusal."""
    db = home / "kanban.db"
    kbc.connect(db).close()
    kbc._INITIALIZED_PATHS.discard(str(db.resolve()))
    _fake_sync_column(monkeypatch)
    assert "kanban_sync_state" in owner._reference_schema(sync=True)
    _foreign_install(home)
    conn = kbc.connect(db)
    try:
        tid = kb.create_task(conn, title="sync off, at par", assignee="default")
        assert conn.execute("SELECT id FROM tasks WHERE id = ?", (tid,)).fetchone()
    finally:
        conn.close()


def test_cli_on_a_missing_board_prints_the_refusal_line_and_creates_nothing(home, capsys):
    """The refusal comes from the CLI's own ``init_db`` (no board yet), not a handler: still one ``kanban:``
    line, not "could not initialize database: kanban: ...", and no empty kanban.db left behind."""
    import argparse
    from hermes_cli import kanban as kcli
    from hermes_cli.kanban_parser import build_parser
    db = home / "kanban.db"
    assert not db.exists()
    _foreign_install(home)
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    rc = kcli.kanban_command(parser.parse_args(["kanban", "list"]))
    err = capsys.readouterr().err
    assert rc == 1
    assert "Traceback" not in err
    lines = [line for line in err.splitlines() if line.strip()]
    assert len(lines) == 1 and lines[0].startswith("kanban: ") and "only this home's installed core" in lines[0]
    assert "could not initialize database" not in lines[0] and not lines[0].startswith("kanban: kanban:")
    assert not db.exists()
    assert not any(p.name.startswith("kanban.db") for p in home.iterdir()), sorted(p.name for p in home.iterdir())


def test_run_forever_give_up_opens_the_hub_card(home, monkeypatch):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_placement as kp
    from hermes_cli import kanban_sync_client as ksc
    from tests.hermes_cli.kanban_sync_fake_board import FakeService
    support.write_sync_config(home, True)
    conn = kbc.connect(home / "kanban.db")
    ksc.ensure_identity(conn, "default", "macbook")
    conn.close()
    monkeypatch.setitem(sync.SYNCED_COLUMNS, "tasks", sync.SYNCED_COLUMNS["tasks"] - {"review_required"})
    hub_cli = FakeHubCli()
    monkeypatch.setattr(kp, "hub_runner", hub_cli)
    svc_hub = FakeService().hub("macbook")
    monkeypatch.setattr(ksc, "load_identity", lambda: svc_hub.ident)
    monkeypatch.setattr(ksc, "Hub", lambda ident: svc_hub)
    monkeypatch.setattr(ksc, "board_paths", lambda: [("default", home / "kanban.db")])
    ticks = iter(range(3))
    ksc.run_forever(lambda: next(ticks, None) is not None, sleep=lambda _s: None)
    (create,) = hub_cli.sent("create-task")
    assert "review_required" in create[create.index("--body") + 1]
