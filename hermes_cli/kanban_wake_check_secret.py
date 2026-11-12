"""Refuse a wake_check that carries an inline secret, for the tool and the CLI park paths alike. [daveey]

A wake_check is executable: storing its redacted form changes what runs (``idempotency_key='review...26b2'``
could never match, t_01a1cf477dece56b), and storing it unredacted keeps the secret on the board. So a probe the
display redactor would alter is refused, never stored altered. Both ``tools/kanban_tools_park_args`` and
``hermes_cli/kanban_cli_park`` call ``secret_error`` before the dry run executes the probe.
"""
from __future__ import annotations

import difflib
import re
from typing import Optional

WAKE_CHECK_SECRET = "wake_check_secret"


def secret_error(cmd: str) -> Optional[str]:
    """Refusal text when ``redact_sensitive_text(cmd, force=True)`` would change ``cmd``, else None.
    The message names only the redacted assignment, never the value."""
    from agent.redact import redact_sensitive_text

    redacted = redact_sensitive_text(str(cmd), force=True)
    if redacted == cmd:
        return None
    raw, red = cmd.split(), redacted.split()
    names = []
    for op, a1, _a2, b1, b2 in difflib.SequenceMatcher(a=raw, b=red, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        m = re.match(r"^['\"]?([A-Za-z0-9_.\-]+)\s*=", red[b1]) if b1 < b2 else None
        names.append(f"`{m.group(1)}=`" if m else f"the value after `{raw[a1 - 1]}`" if a1 else "its first word")
    return (f"{WAKE_CHECK_SECRET}: this wake_check contains a secret or credential-shaped value at "
            f"{', '.join(names[:3])}. Probes run exactly as stored and the board never stores a "
            "secret, so it was not stored, run or masked. Never inline a secret in a probe: read it from env "
            "(`$OPENAI_API_KEY`, set in the probe host's environment) or a file (`curl -H @$HOME/.hub-auth ...`, "
            "`sh ~/bin/check.sh` that fetches it via token-broker), then park again.")
