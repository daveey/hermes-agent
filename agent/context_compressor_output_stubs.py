"""Terminal/execute_code tool-result stubs for context compaction (split from context_compressor).

Lossy stubs say so: a stub reading "1 lines output" for a 16-path listing let the post-compaction agent
guess file names that were never written instead of re-listing the dir.
"""

import json
import re
from typing import Any


def _json_dict(text: Any) -> dict:
    """Parse ``text`` as a JSON object; ``{}`` for empty, invalid, or non-object input."""
    try:
        parsed = json.loads(text) if text else {}
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _str_arg(args: dict, key: str, default: str = "") -> str:
    """Coerce a parsed tool arg to ``str`` (models emit non-string values)."""
    val = args.get(key, default)
    return val if isinstance(val, str) else default if val is None else str(val)


_OUTPUT_ELIDED_NOTE = "(output elided: re-run or list the dir to see it)"
# Outputs this small stay verbatim through the ordinary prune pass (pressure demotion still stubs them).
_KEEP_VERBATIM_MAX_LINES = 12
_KEEP_VERBATIM_MAX_CHARS = 600
_STUB_EDGE_LINE_CHARS = 80


def _decoded_tool_output(content: str) -> str:
    """The ``output`` text of a JSON terminal/execute_code result, else ``content`` itself. Counting
    newlines in the JSON string saw only escaped newlines and reported every output as 1 line."""
    parsed = _json_dict(content)
    if not parsed:
        return content
    output = parsed.get("output")
    return output if isinstance(output, str) else ""


def _output_lines(content: str) -> list[str]:
    text = _decoded_tool_output(content)
    return text.splitlines() if text.strip() else []


def _short_output_kept_verbatim(tool_name: str, content: str) -> bool:
    """True when a terminal/execute_code result's output is a short listing worth keeping whole."""
    if tool_name not in ("terminal", "execute_code"):
        return False
    lines = _output_lines(content)
    return 0 < len(lines) <= _KEEP_VERBATIM_MAX_LINES and sum(len(x) for x in lines) <= _KEEP_VERBATIM_MAX_CHARS


def _output_edges(content: str, content_len: int) -> str:
    """`` (N chars); first: ...; last: ... (output elided ...)``: the stub keeps the output's first and last
    non-blank lines and says the rest was dropped. Contains `` chars)`` so later passes see a stub."""
    lines = [x.strip() for x in _output_lines(content) if x.strip()]

    def clip(x: str) -> str:
        return x if len(x) <= _STUB_EDGE_LINE_CHARS else x[:_STUB_EDGE_LINE_CHARS - 3] + "..."

    if not lines:
        return ""
    edges = f"; first: {clip(lines[0])}"
    if len(lines) > 1:
        edges += f"; last: {clip(lines[-1])}"
    return f" ({content_len:,} chars){edges} {_OUTPUT_ELIDED_NOTE}"


def _sum_terminal(name, args, content, content_len, line_count):
    cmd = _str_arg(args, "command")
    cmd = cmd if len(cmd) <= 80 else cmd[:77] + "..."
    exit_code = m.group(1) if (m := re.search(r'"exit_code"\s*:\s*(-?\d+)', content)) else "?"
    return (
        f"[terminal] ran `{cmd}` -> exit {exit_code}, {line_count} lines output"
        + _output_edges(content, content_len)
    )


def _sum_execute_code(name, args, content, content_len, line_count):
    code_str = _str_arg(args, "code")
    code_preview = code_str[:60].replace("\n", " ") + ("..." if len(code_str) > 60 else "")
    return f"[execute_code] `{code_preview}` ({line_count} lines output)" + _output_edges(content, content_len)
