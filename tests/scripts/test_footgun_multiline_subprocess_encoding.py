"""The ``text=True`` footgun rule must read the whole subprocess call.

A multi-line call that passes ``encoding=`` on a continuation line is not a
footgun; call sites in this tree carried ``# windows-footgun: ok`` markers
only to get past that false positive. A multi-line call that never passes
``encoding=`` must still be flagged.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LINTER_PATH = REPO_ROOT / "scripts" / "check-windows-footguns.py"
RULE = "subprocess text=True without explicit encoding="


def _linter():
    spec = importlib.util.spec_from_file_location("check_windows_footguns_ml", LINTER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_windows_footguns_ml"] = mod
    spec.loader.exec_module(mod)
    return mod


def _hits(tmp_path: Path, source: str) -> list[int]:
    mod = _linter()
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    return [ln for ln, _line, fg in mod.scan_file(path, mod.FOOTGUNS) if fg.name == RULE]


def test_encoding_on_next_line_is_not_flagged(tmp_path):
    src = (
        "import subprocess\n"
        "r = subprocess.run(['git'], capture_output=True, text=True,\n"
        "                   encoding='utf-8', errors='replace')\n"
    )
    assert _hits(tmp_path, src) == []


def test_encoding_several_lines_later_is_not_flagged(tmp_path):
    src = (
        "import subprocess\n"
        "out = subprocess.check_output(\n"
        "    ['git', 'log'], text=True,\n"
        "    cwd='.',\n"
        "    env={'A': '(x'},  # a paren ( in a comment\n"
        "    encoding='utf-8',\n"
        "    errors='replace',\n"
        ")\n"
    )
    assert _hits(tmp_path, src) == []


def test_multiline_call_without_encoding_is_still_flagged(tmp_path):
    src = (
        "import subprocess\n"
        "r = subprocess.run(['git'], capture_output=True, text=True,\n"
        "                   check=False)\n"
        "x = dict(encoding='utf-8')\n"
    )
    assert _hits(tmp_path, src) == [2]


def test_single_line_without_encoding_is_still_flagged(tmp_path):
    src = "import subprocess\nsubprocess.run(['git'], text=True)\nencoding = 'utf-8'\n"
    assert _hits(tmp_path, src) == [2]


def test_unclosed_call_at_eof_is_flagged(tmp_path):
    src = "import subprocess\nsubprocess.run(['git'], text=True,\n"
    assert _hits(tmp_path, src) == [2]


def test_no_stale_multiline_suppression_markers():
    """Guard: once the rule reads continuation lines, a marker that only
    excuses an ``encoding=`` on the next line is dead weight that would hide
    a real future regression on that line."""
    stale = re.compile(r"windows-footgun:\s*ok\b.*(next line|encoding)", re.IGNORECASE)
    offenders = []
    for root in ("hermes_cli", "gateway", "tools", "cron", "agent", "plugins", "scripts", "acp_adapter"):
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for p in base.rglob("*.py"):
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if "text=True" in line and stale.search(line):
                    offenders.append(f"{p.relative_to(REPO_ROOT)}:{n}")
    assert offenders == []
