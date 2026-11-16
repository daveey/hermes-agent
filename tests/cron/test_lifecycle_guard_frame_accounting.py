"""Bound nested-frame accounting work without wall-clock-sensitive assertions."""

import builtins

import pytest

from cron import lifecycle_guard_lines as lines
from cron.lifecycle_guard import _MAX_LIFECYCLE_SCAN_LINE_BYTES


@pytest.mark.parametrize("opener,closer,walker", [
    ("${", "}", lines._parameter_span),
    ("$(", ")", lines._command_span),
])
@pytest.mark.parametrize("unit", ["$(a)", "`a`", "$'a'"])
@pytest.mark.parametrize("collect_breaks", [False, True])
def test_nested_frame_length_work_is_linear(monkeypatch, opener, closer, walker,
                                            unit, collect_breaks):
    # Exactly the existing line cap; neither admission nor refusal policy changes.
    count = (_MAX_LIFECYCLE_SCAN_LINE_BYTES - 3) // len(unit)
    text = opener + unit * count
    text += " " * (_MAX_LIFECYCLE_SCAN_LINE_BYTES - len(text) - 1) + closer
    calls = 0

    def counted_len(value):
        nonlocal calls
        calls += 1
        # Abort quadratic RED runs early, rather than spend minutes summing fragments.
        assert calls <= 50 * _MAX_LIFECYCLE_SCAN_LINE_BYTES
        return builtins.len(value)

    monkeypatch.setattr(lines, "len", counted_len, raising=False)
    breaks = [] if collect_breaks else None
    end, span = walker(text, 2, shlex_quotes=False, breaks=breaks)
    assert (end, span) == (len(text), text)
    assert breaks == ([] if collect_breaks else None)


@pytest.mark.parametrize("opener,closer,walker", [
    ("${", "}", lines._parameter_span),
    ("$(", ")", lines._command_span),
])
@pytest.mark.parametrize("shlex_quotes", [False, True])
def test_break_offsets_follow_normalized_fragments(opener, closer, walker, shlex_quotes):
    # Comments shrink and ANSI-C decoding changes widths before later newlines.
    body = "$(echo a # removed\\\ntrue)\n`a`$'\\x61'\n" * 20
    text = opener + body + closer
    breaks = []
    end, span = walker(text, 2, shlex_quotes=shlex_quotes, breaks=breaks)
    assert end == len(text)
    assert breaks == [i for i, char in enumerate(span) if char == "\n"]
    assert walker(text, 2, shlex_quotes=shlex_quotes) == (end, span)
