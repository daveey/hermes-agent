"""Repo-wide guard: no production code path posts to the Inbox as ``kanban-dispatcher``. [daveey]

The Inbox is reserved for owner-only decisions (AGENTS.md). Dispatcher state (respawn-guard
holds, failing PR checks, bindings) is agent-fixable, so it belongs on the Kanban board as a
comment or a fix card. 1e6f307c7e (2026-10-22) added an Inbox card per respawn-guard hold; David
muted the poster after three unactioned cards. This test fails on that commit's tree.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SKIP_DIRS = {"tests", "node_modules", ".venv", "venv", ".git", "build", "dist", "__pycache__", ".worktrees"}

# `hermes inbox post` as argv (["inbox", "post", ...]), a shell string, or a Python helper.
_INBOX_POST = re.compile(r"""["']inbox["']\s*,\s*["']post["']|\binbox\s+post\b|\binbox_post\s*\(""")
_DISPATCHER_POSTER = re.compile(r"""kanban-dispatcher""")


def offending_files(root: Path) -> list[str]:
    """Non-test ``.py`` files under ``root`` that post to the Inbox and name the dispatcher poster."""
    bad = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if SKIP_DIRS & set(rel.parts[:-1]) or rel.name.startswith("test_"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _INBOX_POST.search(text) and _DISPATCHER_POSTER.search(text):
            bad.append(str(rel))
    return sorted(bad)


# The exact call 1e6f307c7e shipped in hermes_cli/kanban_db_dispatch_guard_notice.py.
_1E6F307C7E_SNIPPET = '''
def _post_inbox_card(task_id: str, title: str, body: str) -> None:
    subprocess.run([sys.executable, "-m", "hermes_cli.main", "inbox", "post", title[:80], "--body", body,
                    "--key", f"guard-hold:{task_id}", "--poster", "kanban-dispatcher"],
                   stdin=subprocess.DEVNULL, capture_output=True, timeout=60, check=False)
'''


def test_detector_flags_the_1e6f307c7e_inbox_post(tmp_path):
    mod = tmp_path / "hermes_cli" / "kanban_db_dispatch_guard_notice.py"
    mod.parent.mkdir()
    mod.write_text(_1E6F307C7E_SNIPPET)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(_1E6F307C7E_SNIPPET)  # tests are exempt
    assert offending_files(tmp_path) == ["hermes_cli/kanban_db_dispatch_guard_notice.py"]


def test_no_production_code_posts_inbox_as_kanban_dispatcher():
    assert offending_files(REPO) == [], (
        "kanban-dispatcher must not post to the Inbox (owner-only decisions, AGENTS.md); "
        "use a kanban comment or a fix card instead")
