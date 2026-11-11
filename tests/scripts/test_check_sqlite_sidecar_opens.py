"""scripts/check_sqlite_sidecar_opens.py: plain file calls on a SQLite -shm/-wal path are flagged unless marked."""

import importlib.util
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_sqlite_sidecar_opens.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_sqlite_sidecar_opens", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "src",
    [
        # hermes-hub 2026-11-11: this exact line released the WAL writer lock on every session-list call.
        'with open(f"{db}-shm", "rb") as f: head = f.read(96)',
        'open(str(db) + "-wal", "rb")',
        'open("state.db", "rb")',
        'os.open("kanban.db", os.O_RDONLY)',
        'Path("cache.db").open("rb")',
        'Path("state.db").read_bytes()',
        'open(f"{db}-journal", "rb")',
        'open(b"state.db", "rb")',
        'db = root / "state.db"\nopen(db)',
        'fd = os.open(f"{db}-shm", os.O_RDONLY)',
        'io.open(db_path + "-wal")',
        'Path(f"{db}-shm").read_bytes()',
        'db.with_name(db.name + "-wal").open("rb")',
        'Path(str(db) + "-shm").write_bytes(b"")',
        'shutil.copyfile(live.with_name(live.name + "-wal"), dest)',
        'shutil.copy2(src=f"{db}-shm", dst=out)',
        'sh.copyfile(db + "-wal", dest)',  # import shutil as sh
        'copyfile(db + "-wal", dest)',  # from shutil import copyfile
        'io.FileIO(f"{db}-shm")',
        'Path(db + "-wal").copy(dest)',  # pathlib, Python 3.14: the receiver is the path
        'Path(db + "-wal").move(dest)',
        'open("%s-wal" % db, "rb")',
        'os.fdopen(os.open(db + "-shm", os.O_RDONLY))',
    ],
)
def test_sidecar_file_calls_are_flagged(src):
    assert _load().sidecar_open_lines(src) == [src.count("\n") + 1]


def test_a_variable_assigned_from_a_sidecar_path_is_followed():
    src = textwrap.dedent("""\
        wal = live.with_name(live.name + "-wal")
        source = wal
        if wal.exists():
            shutil.copyfile(source, dest)
        for shm in [p]:
            pass
        (a, b) = (f"{db}-shm", 1)
        open(a)
    """)
    assert _load().sidecar_open_lines(src) == [4, 8]


@pytest.mark.parametrize(
    "src",
    [
        'self.shm = db + "-shm"\nopen(self.shm)',
        'p = db\np += "-wal"\nopen(p)',
        'for p in (db + "-wal", db + "-shm"):\n    open(p)',
        'heads = [open(p).read() for p in (db + "-wal", db + "-shm")]',
        'SUFFIX = "-wal"\nopen(db + SUFFIX)',
        'with tracked(db + "-shm") as p:\n    open(p)',
        'def f(p=db + "-wal"):\n    open(p)',
        'def f(*, p=db + "-wal"):\n    open(p)',
        'class C:\n    SUFFIX = "-wal"\n    def m(self, db):\n        open(db + self.SUFFIX)',
        'class C:\n    SUFFIX: str = "-wal"\ndef f(db):\n    open(db + C.SUFFIX)',
        'open(db + b"-wal")',
    ],
)
def test_attributes_loop_variables_and_augmented_assignments_are_followed(src):
    assert _load().sidecar_open_lines(src) == [src.count("\n") + 1]


def test_names_are_tracked_per_function_without_flow_and_any_argument_counts():
    """Pinned on purpose: within a function the guard has no flow, so it over-flags rather than
    misses. Tightening it (flow-aware names, path arguments only) must be a deliberate change."""
    mod = _load()
    # Rebinding does not clear a name, and a value derived from a sidecar path keeps the mark.
    assert mod.sidecar_open_lines('p = db + "-wal"\np = other\nopen(p)') == [3]
    assert mod.sidecar_open_lines('wal = db + "-wal"\nlog = wal.parent / "x.log"\nopen(log)') == [3]
    # A tuple assignment marks every target.
    assert mod.sidecar_open_lines('a, b = (f"{db}-shm", 1)\nopen(b)') == [2]
    # Any argument, not only the path, and any receiver: the method name alone decides.
    assert mod.sidecar_open_lines('open(path, encoding="-wal")') == [1]
    assert mod.sidecar_open_lines('game.move(db + "-wal")') == [1]
    # A parameter does not hide a module-level name; a comprehension variable stays marked after it.
    assert mod.sidecar_open_lines('p = db + "-wal"\nf = lambda p: open(p)') == [2]
    assert mod.sidecar_open_lines('names = [p for p in (db + "-wal",)]\nopen(p)') == [2]
    # Module-level names and an enclosing function's names are visible in a function ...
    assert mod.sidecar_open_lines('SUFFIXES = ("", "-wal")\ndef f(db):\n    for s in SUFFIXES:\n        open(db + s)') == [4]
    assert mod.sidecar_open_lines('def f(db):\n    wal = db + "-wal"\n    def g():\n        return open(wal)') == [4]
    # ... but one function's local name says nothing about the same name in another.
    assert mod.sidecar_open_lines('def a(db):\n    path = db + "-wal"\ndef b(path):\n    open(path)') == []
    assert mod.sidecar_open_lines('def a(db):\n    path = db + "-wal"\npath = "x.log"\nopen(path)') == []
    # An attribute set in one method is a sidecar path in every method of the file.
    assert mod.sidecar_open_lines(
        'class C:\n    def a(self, db):\n        self.shm = db + "-shm"\n    def b(self):\n        open(self.shm)') == [5]


@pytest.mark.parametrize(
    "src",
    [
        'pread_lock_safe(f"{db}-shm", 0, 96)',
        'copy_file_out_of_process(f"{db}-wal", dest)',
        'os.stat(f"{db}-wal")',
        'os.stat("state.db")',
        'Path("kanban.db").stat()',
        'os.stat(f"{db}-journal")',
        'sqlite3.connect("state.db")',
        'Path("report.txt").write_text("state.db")',
        'Path("report.txt").write_bytes(b"-journal")',
        'def a():\n    path = "state.db"\ndef b(path):\n    self.output = path\n    Path(self.output).write_text("x")',
        'Path(f"{db}-shm").exists()',
        'os.unlink(str(db) + "-wal")',
        'open(db_path, "rb")',
        'open(log_path)  # rotates like a -wal',
        'doc = "open(db-shm) in a string"',
        'webbrowser.open(url)',
        'name = "-wal"',
        'open(f"{db}-wal-backup")',
        'os.rename(db + "-wal", dest)',
        'if path.endswith("-wal"): open(other)',
        # Documented under "Not caught": a subscript store and an aliased open.
        'd["wal"] = db + "-wal"\nopen(d["wal"])',
        'from os import open as os_open\nos_open(db + "-wal", 0)',
    ],
)
def test_other_shapes_are_not_flagged(src):
    assert _load().sidecar_open_lines(src) == []


def test_marker_on_the_line_or_the_line_above_opts_out(tmp_path):
    mod = _load()
    pkg = tmp_path / "hermes_cli"
    pkg.mkdir()
    f = pkg / "x.py"
    f.write_text(textwrap.dedent("""\
        import shutil
        shutil.copyfile(f"{a}-wal", out)  # sqlite-sidecar-open: ok — offline tool, connections closed
        # sqlite-sidecar-open: ok — detached copy nothing connects to
        shutil.copyfile(f"{b}-wal", out)
        # sqlite-sidecar-open: ok
        shutil.copyfile(f"{c}-wal", out)
        # an unrelated comment
        shutil.copyfile(f"{d}-wal", out)
    """), encoding="utf-8")
    hits = mod.scan([f], root=tmp_path)
    assert [n for n, _ in hits["hermes_cli/x.py"]] == [6, 8]


def test_tests_and_the_helper_are_out_of_scope(tmp_path):
    mod = _load()
    for rel in ("tests/hermes_cli/test_x.py", "hermes_cli/test_y.py", "hermes_cli/sqlite_safe_read.py"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('open(f"{db}-shm", "rb")\n', encoding="utf-8")
    assert mod.scan(list(tmp_path.rglob("*.py")), root=tmp_path) == {}


def test_the_tree_is_clean_and_the_baseline_is_empty():
    mod = _load()
    assert mod._BASELINE == {}
    assert mod.scan() == {}
