"""Quoted shell words must not change printed pipeline structure."""
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from cron.lifecycle_guard_nested_shells import _printed_pipeline_units
from tests.cron import test_lifecycle_guard_session_aliases as aliases

live_kanban_db = aliases.live_kanban_db


@pytest.mark.parametrize("literal", ['"}"', "'}'", r"\}", "''}", '""}', '"fi"', r"\done", "'esac'"])
@pytest.mark.parametrize("separator", [";", "\n"])
def test_literal_closer_does_not_end_printed_group(tmp_path, live_kanban_db, literal, separator):
    text = f"printf 't ( bash {live_kanban_db} )' | {{ echo x; {literal}{separator} bash -l; }}"
    unsafe, refusal = scan_gateway_lifecycle(
        text, cwd=str(tmp_path), session_aliases=frozenset({"zz"}))
    assert unsafe and refusal is not None and "SQLite" in refusal


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("literal", ['"}"', r"\}", "''}"])
def test_real_bash_literal_closer_leaves_pipe_available(tmp_path, literal):
    text = f"printf 'echo FED\\n' | {{ echo x; {literal}; bash --noprofile --norc; }}"
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", text], cwd=tmp_path,
        capture_output=True, text=True, encoding="utf-8", timeout=10, check=True)
    assert result.stdout == "x\nFED\n"
    assert "command not found" in result.stderr


@pytest.mark.parametrize("literal", ['"{"', r"\{", "''{", "'('", r"\(", '"if"', '"case"'])
def test_literal_opener_does_not_join_unrelated_pipelines(tmp_path, live_kanban_db, literal):
    text = f"printf 't ( bash {live_kanban_db} )' | bash; {literal}; printf ':' | bash -l"
    assert scan_gateway_lifecycle(
        text, cwd=str(tmp_path), session_aliases=frozenset({"zz"})) == (False, None)
    assert len(_printed_pipeline_units(text)) == 3


@pytest.mark.parametrize("group", [
    "if :; then {literal}; bash -l; fi",
    "while :; do {literal}; bash -l; break; done",
    "for x in once; do {literal}; bash -l; done",
    "case x in x) {literal}; bash -l;; esac",
    "( case x in (x) {literal}; bash -l;; esac )",
    "{ echo }; {literal}; bash -l; }",
])
@pytest.mark.parametrize("literal", ['"fi"', r"\done", "'esac'", '"}"'])
def test_keyword_compound_keeps_literal_closer(tmp_path, live_kanban_db, group, literal):
    sink = group.replace("{literal}", literal)
    text = f"printf 't ( bash {live_kanban_db} )' | {sink}"
    assert len(_printed_pipeline_units(text)) == 1
    unsafe, refusal = scan_gateway_lifecycle(
        text, cwd=str(tmp_path), session_aliases=frozenset({"zz"}))
    assert unsafe and refusal is not None and "SQLite" in refusal


@pytest.mark.parametrize("word", ['";"', r"\|", "'('", "')'", "'&&'", "''}"])
def test_literal_control_keeps_word_event(word):
    unit, = _printed_pipeline_units(f"echo x; {word}")[-1:]
    assert len(unit.events) == 1
    assert unit.events[0][0] == "word"


@pytest.mark.parametrize("operator", ["|", "|&"])
@pytest.mark.parametrize("continuation", ["\n", " # comment\n", " \\\n"])
def test_pipe_continuation_keeps_unit(operator, continuation):
    assert len(_printed_pipeline_units(f"printf ':' {operator}{continuation}bash -l")) == 1


@pytest.mark.parametrize("redirect", ["2>&1", "0<&3", "&>out", ">|out"])
def test_redirection_does_not_split_pipeline(redirect):
    assert len(_printed_pipeline_units(f"printf ':' {redirect} | bash -l")) == 1


@pytest.mark.parametrize("definition", ["f()", "function f", "function 'f'"])
def test_function_definition_remains_separate_from_call(definition):
    units = _printed_pipeline_units(definition + " { printf ':'; }; f | bash -l")
    assert len(units) >= 2
    assert units[-1].text() == "f | bash -l"


@pytest.mark.parametrize("pattern", ["'esac'", '"("', r"\)", "';;'", '"in"'])
def test_quoted_case_patterns_are_data(tmp_path, live_kanban_db, pattern):
    text = (f"printf 't ( bash {live_kanban_db} )' | "
            f"case x in {pattern}) :;; *) bash -l;; esac")
    unit, = _printed_pipeline_units(text)
    assert sum(role == "open" for role, _ in unit.events) == 1
    assert sum(role == "close" for role, _ in unit.events) == 1
    unsafe, refusal = scan_gateway_lifecycle(
        text, cwd=str(tmp_path), session_aliases=frozenset({"zz"}))
    assert unsafe and refusal is not None and "SQLite" in refusal


@pytest.mark.parametrize("source,expected,quoted", [
    ("}", "}", False), ('"}"', "}", True), ("''}", "}", True),
    (r"\}", "}", True), ("fi", "fi", False), ("f'i'", "fi", True),
    ("''", "", True), (r"\;", ";", True), ("';'", ";", True),
    ("\ue0005\ue000", "\ue0005\ue000", False),
    ("'\ue000'5'\ue000'", "\ue0005\ue000", True),
    ("$'}'", "}", True),
])
def test_lexical_quote_metadata_roundtrip(source, expected, quoted):
    from cron.lifecycle_guard_tokens import _iter_plain_token_lists

    words = list(_iter_plain_token_lists(source))[-1]
    assert len(words) == 1
    assert words[0] == expected
    assert words[0].quoted is quoted


def test_quoted_prefix_does_not_expose_later_group():
    units = _printed_pipeline_units('"time" {; printf : | bash -l')
    assert len(units) == 2
    assert all(role == "word" for role, _ in units[0].events)


@pytest.mark.parametrize("phase,word", [
    ("header", "in"), ("pattern", ")"), ("pattern-word", ")"),
    ("pattern", "("), ("body", ";;"), ("pattern", "esac"),
])
def test_case_phase_ignores_literal_syntax(phase, word):
    from cron.lifecycle_guard_nested_shells import _case_word_role
    from cron.lifecycle_guard_words import ShellWord

    cases = {1: phase}
    result = _case_word_role(cases, 1, ShellWord(word, None, quoted=True))
    assert cases == {1: phase}
    assert result == (None if phase == "body" else "data")
    plain = {1: phase}
    plain_result = _case_word_role(plain, 1, word)
    assert plain != cases or plain_result != result
