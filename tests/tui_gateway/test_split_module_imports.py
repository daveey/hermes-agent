"""Guard: helpers of a ``bind_module`` split module are reached through ``server``, never imported directly. [daveey]

A split module (``tui_gateway/<name>.py`` that calls ``bind_module(globals(), server)``) is written against
server.py's namespace: its functions use names such as ``_get_db``, ``Path`` or ``logger`` that only exist after
``bind_module`` rebinds them onto server globals. The module object itself keeps the raw, un-rebound copies, so
``from tui_gateway.session_workdir import _session_db`` imports a function that raises ``NameError`` (or, under a
``contextlib.suppress``, silently does less) the first time it touches one of those names. turn_outcome.py did
exactly that and every Desktop turn's outcome refresh failed. Reach split helpers as ``server._name`` instead.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "tui_gateway"
EXEMPT = {"server.py", "method_ctx.py"}


def _calls_bind_module(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if (getattr(fn, "id", None) or getattr(fn, "attr", None)) == "bind_module":
                return True
    return False


def _split_modules() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path in ROOT.glob("*.py"):
        if path.name in EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _calls_bind_module(tree):
            out[path.stem] = {
                n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return out


def _imported_split_module(node: ast.ImportFrom) -> str | None:
    if node.level == 1 and node.module and "." not in node.module:
        return node.module
    if node.level == 0 and node.module and node.module.startswith("tui_gateway."):
        rest = node.module.split(".", 1)[1]
        return rest if "." not in rest else None
    return None


def _violations() -> list[str]:
    split = _split_modules()
    found = []
    for path in sorted(ROOT.glob("*.py")):
        if path.name == "server.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ImportFrom):
                continue
            mod = _imported_split_module(node)
            if mod not in split or mod == path.stem:
                continue
            for alias in node.names:
                if alias.name in split[mod]:
                    found.append(f"tui_gateway/{path.name}:{node.lineno} imports {mod}.{alias.name}")
    return found


def test_split_module_discovery_sees_the_known_split_modules():
    split = _split_modules()
    assert "_session_db" in split.get("session_workdir", set())
    assert "_bot_mode_delivery_text" in split.get("prompt_turn", set())
    assert "_readopt_wake_profiles" in split.get("session_notifications", set())


def test_no_direct_imports_of_split_module_functions():
    bad = _violations()
    assert not bad, (
        "split-module helpers must be reached through `server` (from tui_gateway import server; "
        "server._name(...)), not imported directly; their raw module copies lack server's globals:\n  "
        + "\n  ".join(bad)
    )


def test_guard_flags_the_original_turn_outcome_import(tmp_path, monkeypatch):
    """The detector itself goes red on the import that shipped the NameError."""
    import shutil

    fake = tmp_path / "tui_gateway"
    fake.mkdir()
    shutil.copy(ROOT / "session_workdir.py", fake / "session_workdir.py")
    (fake / "turn_outcome.py").write_text(
        "def f(s):\n    from tui_gateway.session_workdir import _session_db\n    return _session_db(s)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "ROOT", fake)
    assert _violations() == ["tui_gateway/turn_outcome.py:2 imports session_workdir._session_db"]
