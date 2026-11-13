"""HX901: a ``@contextmanager`` that swallows the caller's error at its ``yield`` and can then yield again
(registered in ``py_rules.CHECKERS``).

An exception raised in the caller's ``with`` block is thrown INTO the generator at the ``yield``. A broad
``except`` around that ``yield`` that does not re-raise eats the real error; when the generator then yields
again (a loop's next candidate, a fallback ``yield None``) Python raises "generator didn't stop after
throw()" and the real cause is gone (kanban-e5babca565: a chat mid-turn logged as a crash every 30 s for 2h).
A one-shot suppressor (one ``yield`` in a ``try``, outside any loop: ``contextlib.suppress`` that logs) is a
deliberate design and is not flagged. The fix shape: find the resource inside the ``try``, then yield it
outside any handler (an ``ExitStack`` keeps what was opened alive across the ``yield``).
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator

from scripts.code_health.py_ast import _FUNCS, Ctx, _dotted

_CM_DECORATORS = {"contextmanager", "asynccontextmanager"}
_BROAD = {"Exception", "BaseException"}


def _is_cm(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in func.decorator_list:
        name = _dotted(dec.func if isinstance(dec, ast.Call) else dec)
        if name.rsplit(".", 1)[-1] in _CM_DECORATORS:
            return True
    return False


def _own(nodes: list[ast.stmt]) -> Iterator[ast.AST]:
    """Every node under ``nodes`` that runs in this frame (nested defs/lambdas/classes are other frames)."""
    stack: list[ast.AST] = list(nodes)
    while stack:
        node = stack.pop()
        yield node
        stack.extend(child for child in ast.iter_child_nodes(node)
                     if not isinstance(child, (*_FUNCS, ast.Lambda, ast.ClassDef)))


def _broad(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(_dotted(t).rsplit(".", 1)[-1] in _BROAD for t in types)


def _yields(nodes: list[ast.stmt]) -> bool:
    return any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in _own(nodes))


def _reraises(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(n, ast.Raise) for n in _own(handler.body))


def _can_yield_again(func: ast.FunctionDef | ast.AsyncFunctionDef, try_node: ast.AST) -> bool:
    """More than one ``yield`` in the frame, or the ``try`` sits in a loop (its next pass yields again)."""
    if sum(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in _own(func.body)) > 1:
        return True
    loops = (n for n in _own(func.body) if isinstance(n, (ast.For, ast.AsyncFor, ast.While)))
    return any(try_node in set(_own(loop.body)) for loop in loops)


def swallowed_yield(tree: ast.Module, ctx: Ctx) -> Iterable[int]:
    for func in ast.walk(tree):
        if not isinstance(func, _FUNCS) or not _is_cm(func):
            continue
        for node in _own(func.body):
            if (not isinstance(node, (ast.Try, ast.TryStar)) or not _yields(node.body)
                    or not _can_yield_again(func, node)):
                continue
            for handler in node.handlers:
                if _broad(handler) and not _reraises(handler):
                    yield handler.lineno
