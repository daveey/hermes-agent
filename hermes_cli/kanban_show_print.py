"""Human rendering shared by ``hermes kanban show`` and ``diagnostics`` (split out of ``kanban``)."""
from __future__ import annotations

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_ops import _kanban_config


def _print_diagnostics(diags, indent: str, *, with_kind: bool) -> None:
    """Shared human rendering for ``show`` and ``diagnostics`` (suggested actions only)."""
    sev_marker = {"warning": "⚠", "error": "!!", "critical": "!!!"}
    for d in diags:
        head = f"{d.kind}: {d.title}" if with_kind else d.title
        print(f"{indent}{sev_marker.get(d.severity, '?')} [{d.severity}] {head}")
        if d.data:
            bits = [f"{k}={','.join(str(x) for x in v)}" if isinstance(v, list) else f"{k}={v}"
                    for k, v in d.data.items()]
            if bits:
                print(f"{indent}   data: {' | '.join(bits)}")
        for a in d.actions:
            if a.suggested:
                print(f"{indent}   → {a.label}")


def _print_section(title: str, lines) -> None:
    """Blank line, ``title``, then each line (``show`` body sections)."""
    print()
    print(title)
    for line in lines:
        print(line)


def _print_max_retries(task) -> None:
    """Effective retry threshold (task > config > default); it explains auto-blocks."""
    if task.max_retries is not None:
        print(f"  max-retries: {task.max_retries} (task)")
        return
    cfg_val = _kanban_config().get("failure_limit")
    if cfg_val is not None and int(cfg_val) != kb.DEFAULT_FAILURE_LIMIT:
        print(f"  max-retries: {int(cfg_val)} (config kanban.failure_limit)")
    else:
        print(f"  max-retries: {kb.DEFAULT_FAILURE_LIMIT} (default)")
