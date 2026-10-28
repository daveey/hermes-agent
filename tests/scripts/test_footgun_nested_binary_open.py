"""open(os.path.join(...), "rb") is binary, even when the mode regex stops at the nested comma."""
import importlib.util
import sys
from pathlib import Path

import pytest

OPEN = "open() without encoding= on text mode"


@pytest.fixture(scope="module")
def linter():
    path = Path(__file__).resolve().parents[2] / "scripts/check-windows-footguns.py"
    spec = importlib.util.spec_from_file_location("nested_open_footguns", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("source,flagged", [
    ('with open(os.path.join(sys.prefix, "pyvenv.cfg"), "rb") as fh:', False),
    ("open(os.path.join(a, b), 'wb')", False),
    ("open(path, 'rb')", False),
    ("open(path, 'w')", True),
    ("open(path)", True),
    ("open(os.path.join(a, b), 'w')", True),
    ('open(os.path.join(a, b), "rb")  # not a text read', False),
    ('open(path, "w")  # was "rb" once', True),
])
def test_nested_binary_open_is_not_a_text_footgun(linter, tmp_path, source, flagged):
    path = tmp_path / "input.py"
    path.write_text(source + "\n", encoding="utf-8")
    rules = [rule for rule in linter.FOOTGUNS if rule.name == OPEN]
    assert len(rules) == 1
    hits = linter.scan_file(path, rules)
    assert bool(hits) is flagged
