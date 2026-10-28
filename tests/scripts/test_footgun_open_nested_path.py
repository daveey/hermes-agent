"""The ``open() without encoding= on text mode`` rule reads the mode past a call in the path.

``open(os.path.join(sys.prefix, "pyvenv.cfg"), "rb")`` failed CI's Windows footgun scan: the
path matcher stopped at the comma inside ``os.path.join(...)``, never saw ``"rb"``, and called it
text mode.
"""

from __future__ import annotations

import pytest

from tests.scripts.test_footgun_subprocess_encoding import _load_linter_module, _scan_line

RULE_NAME = "open() without encoding= on text mode"


@pytest.fixture(scope="module")
def linter():
    return _load_linter_module()


@pytest.mark.parametrize("line", [
    'with open(os.path.join(sys.prefix, "pyvenv.cfg"), "rb") as fh:',
    'data = open(Path(a, b), "wb")',
    'with open(os.path.join(root, name), encoding="utf-8") as fh:',
])
def test_binary_or_encoded_open_with_a_call_in_the_path_is_not_flagged(linter, line):
    assert not _scan_line(linter, line, RULE_NAME)


@pytest.mark.parametrize("line", [
    'with open(os.path.join(root, name)) as fh:',
    'with open(os.path.join(root, name), "r") as fh:',
    'with open(path, "w") as fh:',
])
def test_text_open_is_still_flagged(linter, line):
    assert _scan_line(linter, line, RULE_NAME)
