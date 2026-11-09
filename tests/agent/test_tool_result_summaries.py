"""Compaction stubs for terminal/execute_code results must say what they dropped.

Regression for a hub session where 16 extracted figure paths compacted to
"[terminal] ran ... -> exit 0, 1 lines output": the line count read the JSON-escaped
newlines and no path survived, so the agent guessed four file names that never existed.
"""

import json

from agent.context_compressor import (
    ContextCompressor,
    _is_summary_stub,
    _summarize_tool_result,
)

PATHS = [f"/home/ubuntu/scratch/beam/figure-page-{n}-0.png" for n in range(3, 150, 9)][:16]
ELIDED = "(output elided: re-run or list the dir to see it)"


def _terminal_result(lines):
    return json.dumps({"output": "\n".join(lines), "exit_code": 0, "error": None})


def _demote(tool_name, args, content):
    call = {"id": "c1", "type": "function", "function": {"name": tool_name, "arguments": json.dumps(args)}}
    result = [
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "c1", "content": content},
    ]
    changed = ContextCompressor._demote_tool_result_at(
        result, 1, {"c1": (tool_name, json.dumps(args))}, 200, set(),
    )
    return changed, result[1]["content"]


def test_terminal_stub_counts_decoded_output_lines_and_keeps_first_and_last():
    assert len(PATHS) == 16
    stub = _summarize_tool_result("terminal", json.dumps({"command": "python3 extract.py"}), _terminal_result(PATHS))
    assert "16 lines output" in stub
    assert PATHS[0] in stub
    assert PATHS[-1] in stub
    assert stub.endswith(ELIDED)
    assert len(stub) < 400 and _is_summary_stub(stub), "a later pass must recognize the stub, not re-summarize it"


def test_execute_code_stub_counts_decoded_lines_and_says_output_was_elided():
    content = json.dumps({"status": "success", "output": "\n".join(PATHS), "tool_calls_made": 0})
    stub = _summarize_tool_result("execute_code", json.dumps({"code": "print(paths)"}), content)
    assert "16 lines output" in stub
    assert PATHS[0] in stub and PATHS[-1] in stub
    assert stub.endswith(ELIDED)


def test_non_json_terminal_content_falls_back_to_raw_text():
    stub = _summarize_tool_result("terminal", json.dumps({"command": "ls"}), "a\nb\nc")
    assert "3 lines output" in stub


def test_short_terminal_listing_is_kept_verbatim_by_the_ordinary_prune_pass():
    short = PATHS[:6]
    content = _terminal_result(short)
    assert len(content) > 200
    changed, kept = _demote("terminal", {"command": "ls figures"}, content)
    assert not changed
    assert kept == content


def test_long_terminal_listing_is_demoted_to_an_honest_stub():
    changed, stub = _demote("terminal", {"command": "ls figures"}, _terminal_result(PATHS))
    assert changed
    assert "16 lines output" in stub and PATHS[0] in stub and PATHS[-1] in stub
    assert stub.endswith(ELIDED)


def test_json_result_without_output_field_is_not_reported_as_output():
    stub = _summarize_tool_result("terminal", json.dumps({"command": "true"}), '{"exit_code": 0}')
    assert "0 lines output" in stub
    assert "exit_code" not in stub.split("->", 1)[1]
    assert ELIDED not in stub
