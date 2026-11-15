"""Parser work is reused only within one traversal and one nesting budget."""
from collections import Counter

import pytest

from cron import lifecycle_guard_tokens as tokens
from cron.lifecycle_guard_substitutions import MAX_SUBSTITUTION_DEPTH, SubstitutionNestingLimit


def test_repeated_bodies_are_walked_once(monkeypatch):
    calls = Counter()
    original = tokens.separate_substitutions

    def counted(command, **kwargs):
        if not kwargs:
            calls[command] += 1
        return original(command, **kwargs)

    monkeypatch.setattr(tokens, 'separate_substitutions', counted)
    command = 'echo $(echo $(bash ./r.sh)) $(echo $(bash ./r.sh))'
    result = list(tokens._iter_command_token_lists(command))
    assert ['bash', './r.sh'] in result
    assert calls['echo $(bash ./r.sh)'] == 1
    assert calls['bash ./r.sh'] == 1
    list(tokens._iter_command_token_lists(command))
    assert calls['bash ./r.sh'] == 2  # no process-wide cache


def test_convergent_readings_are_tokenized_once(monkeypatch):
    calls = Counter()
    original = tokens._iter_raw_token_lists

    def counted(command):
        calls[command] += 1
        yield from original(command)

    # Original, empty and placeholder readings differ before preprocessing,
    # but comments / continuation removal make all three identical.
    monkeypatch.setattr(tokens, 'separate_substitutions', lambda command, **kw:
                        ('echo ok # empty' if kw else 'echo ok # outer', []))
    monkeypatch.setattr(tokens, '_iter_raw_token_lists', counted)
    assert list(tokens._iter_command_token_lists('echo ok # original')) == [['echo', 'ok']]
    assert sum(calls.values()) == 1


def test_distinct_readings_and_ansi_normalization_survive(monkeypatch):
    # The raw reading decodes ANSI-C escapes; normalization keeps them literal.
    monkeypatch.setattr(tokens, 'separate_substitutions', lambda command, **kw:
                        ("echo $'empty\\x41'" if kw else "echo $'outer\\x41'", []))
    result = list(tokens._iter_command_token_lists("echo $'original\\x41'"))
    assert {tuple(row) for row in result} == {
        ('echo', 'originalA'), ('echo', 'original\\x41'),
        ('echo', 'emptyA'), ('echo', 'empty\\x41'),
        ('echo', 'outerA'), ('echo', 'outer\\x41'),
    }


def test_cached_occurrences_preserve_order_and_list_isolation():
    rows = tokens._iter_command_token_lists('echo $(true) $(false) $(true)')
    first = next(rows)
    assert first == ['true']
    first.append('mutated')
    assert next(rows) == ['false']
    assert next(rows) == ['true']


def test_plain_readings_default_keeps_distinct_ansi_evidence():
    assert list(tokens._iter_plain_token_lists("echo $'o\\x6b'")) == [
        ['echo', 'ok'], ['echo', 'o\\x6b'],
    ]


def test_repeated_body_at_deeper_budget_still_hits_limit():
    body = 'echo $(true)'
    deep = body
    for _ in range(MAX_SUBSTITUTION_DEPTH - 2):
        deep = 'echo $(' + deep + ')'
    with pytest.raises(SubstitutionNestingLimit):
        list(tokens._iter_command_token_lists('echo $(' + body + ') $(' + deep + ')'))
