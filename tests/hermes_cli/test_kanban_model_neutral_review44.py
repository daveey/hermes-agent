"""Regression controls for negated mandates and lossless historical resubmission."""
import json
import pytest
from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_containment_lifecycle as chelpers

board = chelpers.board

NEGATED = [
    ("no_longer", "This review no longer requires Opus."),
    ("does_not_require", "Review does not require claude-opus-5-5 any more."),
    ("non_mandatory", "Re-run your repro (the `_deep` fixture, codex live wall 55 h out, fresh non-mandatory astra pin, hourly ticks)."),
    ("remove_text", "Remove the skill text that requires Opus review."),
]
REAL_ROWS = [
    ("t_01a1ff099ea17a5b", "Checked: sqlite tasks query — ('t_c850e0f6', 'archived', 'default', 'Ask Eve workflow (d): real Slack A–I E2E + mandatory Opus/neutral reviews')."),
    ("t_01a1ea59e416b901_pin_sweep", "- `t_c850e0f6` Ask Eve workflow (d): real Slack A–I E2E + mandatory Opus/neutral reviews (structural, left-hub): pin_sweep.py check t_c850e0f6: no_work=false"),
    ("t_01a1fce107fedddd", "Author model custody is gpt-6.1-sol/openai-codex only for actual source authorship;"),
]
CONTROLS = ["This review MUST run claude-opus-5-5/anthropic, mandatory pin.", "Use claude-opus-5-5.", "require_model: true"]


@pytest.mark.parametrize("text", [t for _n, t in NEGATED], ids=[n for n, _t in NEGATED])
def test_creates(board, text):
    tid = kb.create_task(board, title="Follow-up card", body=f"Context.\n{text}\nMore.", assignee="default")
    assert kb.get_task(board, tid).body.count(text) == 1


@pytest.mark.parametrize("text", CONTROLS)
def test_control_refused(board, text):
    with pytest.raises(ValueError):
        kb.create_task(board, title="Review card", body=text, assignee="default")


@pytest.mark.parametrize("text", [t for _n, t in REAL_ROWS])
def test_evidence_label_carries_line(board, text):
    line = "Historical run instruction (superseded and not active): " + json.dumps(text, ensure_ascii=False)
    tid = kb.create_task(board, title="Follow-up card", body=line, assignee="default")
    assert kb.get_task(board, tid).body == line


@pytest.mark.parametrize("text", [t for _n, t in REAL_ROWS] + CONTROLS)
def test_error_names_the_quoting_form_and_resubmission_is_lossless(board, text):
    before = board.execute('SELECT count(*) FROM tasks').fetchone()[0]
    with pytest.raises(ValueError) as err:
        kb.create_task(board, title="t", body=text, assignee="default")
    assert board.execute('SELECT count(*) FROM tasks').fetchone()[0] == before
    label = "Historical run instruction (superseded and not active): "
    assert label in str(err.value)
    assert text in str(err.value)
    line = label + json.dumps(text, ensure_ascii=False)
    tid = kb.create_task(board, title="Historical evidence", body=line, assignee="default")
    task = kb.get_task(board, tid)
    assert task is not None
    body = task.body
    assert body == line
    assert body is not None
    assert json.loads(body[len(label):]) == text


@pytest.mark.parametrize("text", [t for _n, t in NEGATED])
@pytest.mark.parametrize("suffix", ["; Use claude-opus-5-5.", ": Use claude-opus-5-5.",
                                  " and must run claude-opus-5-5.", "\nrequire_model: true"])
def test_negative_phrase_never_masks_appended_positive_mandate(board, text, suffix):
    before = board.execute('SELECT count(*) FROM tasks').fetchone()[0]
    with pytest.raises(ValueError):
        kb.create_task(board, title="t", body=text + suffix, assignee="default")
    assert board.execute('SELECT count(*) FROM tasks').fetchone()[0] == before


@pytest.mark.parametrize("suffix", [': Use claude-opus-5-5.', '\nrequire_model: true'])
def test_supported_evidence_still_checks_trailing_active_instruction(board, suffix):
    line = 'Historical run instruction (superseded and not active): ' + json.dumps(REAL_ROWS[0][1])
    with pytest.raises(ValueError):
        kb.create_task(board, title="t", body=line + suffix, assignee="default")

