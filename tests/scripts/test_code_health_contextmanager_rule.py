"""HX901: a ``@contextmanager`` whose broad ``except`` around ``yield`` swallows the caller's error.

kanban-e5babca565: ``tui_gateway.kanban_unloaded.find_session_db`` caught the ``with`` block's own
"Session has an active turn lease" at its ``yield``, yielded again, and the sweeper logged "generator didn't
stop after throw()" every 30 s for 2h. The rule flags the shape; the behaviour itself is pinned in
tests/tui_gateway/test_kanban_unloaded_busy.py.
"""

from __future__ import annotations

import ast
import contextlib

import pytest

from scripts.code_health.config import RULES_BY_ID, rule_applies
from scripts.code_health.py_rules import CHECKERS, Ctx, canonical_tree

_FIXED_FIND = '''
import contextlib
@contextlib.contextmanager
def find(stores):
    with contextlib.ExitStack() as stack:
        found = None
        for s in stores:
            try:
                found = stack.enter_context(s.open())
                break
            except Exception:
                pass
        yield found
'''


def _hits(src: str) -> list[int]:
    return sorted(set(CHECKERS["HX901"](canonical_tree(ast.parse(src)), Ctx())))


@pytest.mark.parametrize("src, flagged", [
    # the original find_session_db shape
    ("import contextlib\n@contextlib.contextmanager\ndef f(ps):\n    for p in ps:\n        try:\n"
     "            with p.open() as db:\n                yield db\n                return\n"
     "        except Exception:\n            pass\n    yield None\n", True),
    ("from contextlib import contextmanager\n@contextmanager\ndef f():\n    try:\n        yield 1\n"
     "    except:\n        log()\n    yield None\n", True),
    ("from contextlib import asynccontextmanager\n@asynccontextmanager\nasync def f(ps):\n    for p in ps:\n"
     "        try:\n            yield p\n            return\n        except (ValueError, BaseException):\n"
     "            pass\n", True),  # one yield, but the loop's next pass reaches it again
    # a one-shot suppressor (suppress-and-log) yields once and ends: deliberate, not flagged
    ("from contextlib import contextmanager\n@contextmanager\ndef quiet(label):\n    try:\n        yield\n"
     "    except Exception:\n        log(label)\n", False),
    # re-raising, a narrow catch, a finally, or a yield outside the try are all fine
    ("from contextlib import contextmanager\n@contextmanager\ndef f():\n    try:\n        yield 1\n"
     "    except Exception:\n        cleanup()\n        raise\n", False),
    ("from contextlib import contextmanager\n@contextmanager\ndef f():\n    try:\n        yield 1\n"
     "    except KeyError:\n        pass\n", False),
    ("from contextlib import contextmanager\n@contextmanager\ndef f():\n    try:\n        yield 1\n"
     "    finally:\n        close()\n", False),
    (_FIXED_FIND, False),
    # not a context manager: a plain generator may legitimately catch around yield
    ("def gen():\n    try:\n        yield 1\n    except Exception:\n        pass\n", False),
    # a yield in a nested def is another frame
    ("from contextlib import contextmanager\n@contextmanager\ndef f():\n    try:\n"
     "        def g():\n            yield 1\n        x = g()\n    except Exception:\n        pass\n"
     "    yield x\n", False),
])
def test_swallowed_yield_shapes(src, flagged):
    assert bool(_hits(src)) is flagged, src


def test_flagged_shape_really_hides_the_callers_error():
    """The contract the rule encodes: the flagged shape turns the caller's error into RuntimeError."""
    ns: dict = {"contextlib": contextlib}

    class _Store:
        @contextlib.contextmanager
        def open(self):
            yield "db"

    exec("@contextlib.contextmanager\ndef f(ps):\n    for p in ps:\n        try:\n"
         "            with p.open() as db:\n                yield db\n                return\n"
         "        except Exception:\n            pass\n    yield None\n", ns)
    exec(_FIXED_FIND, ns)
    with pytest.raises(RuntimeError, match="didn't stop after throw"):
        with ns["f"]([_Store()]):
            raise KeyError("busy")
    with pytest.raises(KeyError):
        with ns["find"]([_Store()]):
            raise KeyError("busy")


def test_rule_covers_product_code_not_tests():
    rule = RULES_BY_ID["HX901"]
    assert rule_applies(rule, "tui_gateway/kanban_unloaded.py")
    assert rule_applies(rule, "hermes_cli/kanban_db.py")
    assert not rule_applies(rule, "tests/tui_gateway/test_x.py")
