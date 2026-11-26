"""Best-effort update-time warming of source bytecode, never application imports."""
from __future__ import annotations

import logging
from pathlib import Path
import subprocess
import sys

from hermes_cli._subprocess_compat import windows_hide_flags

logger = logging.getLogger(__name__)
_IMPORT_ROOTS = ("hermes_cli", "agent", "tools", "tui_gateway", "gateway", "cron", "pm", "plugins", "acp_adapter")


def precompile_checkout(root: Path) -> None:
    """Warm the prepared interpreter's cache before restarting services.

    Only update completion calls this: launch-time warming would add to startup
    latency. A 30-second cap may leave partial caches; imports still fall back to
    source. Older updater/interpreter paths may create a different cache tag;
    that cache is harmless but will not accelerate the later interpreter.
    Isolated mode ignores ambient PYTHONPYCACHEPREFIX/PYTHONDONTWRITEBYTECODE;
    the completion parent's temporary -X pycache_prefix is not inherited.
    Single-worker compileall has no pool descendants to orphan on timeout.
    """
    try:
        root = root.resolve()
        targets = [root / name for name in _IMPORT_ROOTS]
        targets.extend(sorted(root.glob("*.py")))
        targets = [path for path in targets if not path.is_symlink() and path.exists()]
        if not targets:
            return
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-m", "compileall", "-q", "-j", "1",
             "-e", str(root), *map(str, targets)],
            cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=30, check=False,
            creationflags=windows_hide_flags(),
        )
        if result.returncode:
            logger.debug("Update bytecode warming incomplete (exit %s)", result.returncode)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Update bytecode warming skipped: %s", exc)
