"""review_required plumbing: parser, CLI/tool create, edit, migration default, sync capture, remote forwarding."""
import argparse
import json


import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_operator as kbo
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_placement as placement
from hermes_cli import kanban_placement_forward as forward


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB",
                "HERMES_KANBAN_BOARD", "HERMES_SESSION_ID", "HERMES_KANBAN_WORKER_TASK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(kc, "_check_dispatcher_presence", lambda **kw: (True, ""))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    from tests.hermes_cli.test_kanban_selfwork_entry import register_chat
    monkeypatch.setenv('HERMES_SESSION_ID', register_chat(h, '20261123_120000_review'))
    return h


def parse(*argv):
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    return root.parse_args(["kanban", *argv])


def get(conn, task_id):
    task = kb.get_task(conn, task_id)
    assert task is not None
    return task


def flag(task_id):
    with kbc.connect_closing() as conn:
        return get(conn, task_id).review_required


def make(review_required=False):
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title="plumbing fixture", assignee="default",
                              review_required=review_required)


# --- parser ---

def test_create_parser_flag_default_and_set():
    assert parse("create", "t", "--assignee", "default").review_required is False
    assert parse("create", "t", "--assignee", "default", "--review-required").review_required is True


def test_edit_parser_accepts_on_off_and_rejects_other():
    assert parse("edit", "t_1", "--review-required", "on").review_required == "on"
    assert parse("edit", "t_1", "--review-required", "off").review_required == "off"
    assert parse("edit", "t_1").review_required is None
    with pytest.raises(SystemExit):
        parse("edit", "t_1", "--review-required", "maybe")


# --- CLI handlers ---

@pytest.mark.parametrize("extra,expected", [([], False), (["--review-required"], True)])
def test_cli_create_handler_persists_flag(home, extra, expected):
    args = parse("create", "CLI review plumbing", "--assignee", "default", "--json", *extra)
    assert kc._cmd_create(args) == 0
    with kbc.connect_closing() as conn:
        (task,) = [t for t in kb.list_tasks(conn) if t.title == "CLI review plumbing"]
    assert task.review_required is expected


def test_cli_edit_handler_enables_then_disables_for_operator(home, capsys):
    tid = make()
    assert kc._cmd_edit(parse("edit", tid, "--review-required", "on")) == 0
    assert flag(tid) is True
    assert "review_required" in capsys.readouterr().out
    assert kc._cmd_edit(parse("edit", tid, "--review-required", "off")) == 0
    assert flag(tid) is False


def test_cli_edit_review_required_alone_is_a_valid_edit(home):
    tid = make()
    assert kc._cmd_edit(parse("edit", tid, "--review-required", "on")) == 0  # no "provide --title..." error


@pytest.mark.parametrize("identity", ["HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKER_TASK"])
def test_cli_edit_worker_cannot_disable(home, monkeypatch, capsys, identity):
    tid = make(True)
    monkeypatch.setenv(identity, tid)
    assert kc._cmd_edit(parse("edit", tid, "--review-required", "off")) != 0
    assert "an operator (outside a kanban worker run)" in capsys.readouterr().err
    assert flag(tid) is True


# --- operator edit (DB layer) ---

def test_operator_edit_worker_cannot_disable_but_can_enable(home, monkeypatch):
    tid = make(True)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    with kbc.connect_closing() as conn:
        with pytest.raises(kbo.KanbanEditRefused):
            kbo.edit_operator_fields(conn, tid, review_required=False)
        assert get(conn, tid).review_required is True
    other = make(False)
    with kbc.connect_closing() as conn:
        changed = kbo.edit_operator_fields(conn, other, review_required=True)
        assert changed is not None
        assert "review_required" in changed
        assert get(conn, other).review_required is True


def test_operator_edit_non_worker_can_disable(home):
    tid = make(True)
    with kbc.connect_closing() as conn:
        changed = kbo.edit_operator_fields(conn, tid, review_required=False)
        assert changed is not None
        assert changed["review_required"]["new"] in (0, False)
        assert get(conn, tid).review_required is False


# --- tool create ---

def test_tool_create_flag_true_and_default_false(home):
    from tools import kanban_tools as kt
    on = json.loads(kt._handle_create({"title": "tool review on", "assignee": "default", "review_required": True}))
    off = json.loads(kt._handle_create({"title": "tool review default", "assignee": "default"}))
    assert not on.get("error"), on
    assert not off.get("error"), off
    assert flag(on["task_id"]) is True
    assert flag(off["task_id"]) is False


def test_tool_schema_declares_boolean_flag():
    from tools import kanban_tools_schemas as schemas
    props = schemas.KANBAN_CREATE_SCHEMA["parameters"]["properties"]
    assert props["review_required"]["type"] == "boolean"


# --- migration ---

def test_migration_adds_column_default_false_to_legacy_db(home):
    path = kb.kanban_db_path()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="legacy row", assignee="default")
        # Reconstruct the pre-column schema, including its pre-column trigger set.
        triggers = conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='tasks'").fetchall()
        for row in triggers:
            conn.execute('DROP TRIGGER "' + row[0].replace('"', '""') + '"')
        conn.execute("ALTER TABLE tasks DROP COLUMN review_required")
        conn.commit()
        assert "review_required" not in [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect_closing() as conn:
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "review_required" in cols
        assert cols["review_required"][3] == 1 and str(cols["review_required"][4]) == "0"  # NOT NULL DEFAULT 0
        assert get(conn, tid).review_required is False
    assert path


def test_new_task_default_is_false(home):
    assert flag(make()) is False
    assert flag(make(True)) is True


# --- sync capture ---

def test_sync_declares_and_captures_review_required(home):
    assert "review_required" in sync.SYNCED_COLUMNS["tasks"]
    with kbc.connect_closing() as conn:
        assert "review_required" in sync.captured_columns(conn, "tasks")
        assert "review_required" not in sync.undeclared_columns(conn).get("tasks", set())


# --- remote forwarding ---

def test_carried_options_keeps_true_and_drops_default():
    assert "review_required" in placement.CARRIED_OPTIONS
    assert placement.carried_options({"title": "t", "review_required": True})["review_required"] is True
    assert "review_required" not in placement.carried_options({"title": "t", "review_required": False})
    assert "review_required" not in placement.carried_options({"title": "t"})


def test_legacy_clients_without_option_support_cannot_carry_flag():
    assert "review_required" in placement.LEGACY_UNCARRIED


@pytest.mark.parametrize("required", [True, False])
def test_remote_args_preserves_flag(home, required):
    tid = make(required)
    with kbc.connect_closing() as conn:
        task = get(conn, tid)
        args = forward.remote_args(conn, task, self_gw="gw-a")
    assert args["review_required"] is required
