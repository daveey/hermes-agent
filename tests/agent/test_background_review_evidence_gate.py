"""[daveey] Tests for the optional evidence gate (auxiliary.background_review.require_evidence)."""

from unittest.mock import MagicMock

from agent.background_review import (
    _COMBINED_REVIEW_PROMPT,
    _EVIDENCE_GATE,
    _MEMORY_REVIEW_PROMPT,
    _SKILL_REVIEW_PROMPT,
    spawn_background_review_thread,
)


def _bare_agent():
    agent = MagicMock()
    del agent._COMBINED_REVIEW_PROMPT
    del agent._MEMORY_REVIEW_PROMPT
    del agent._SKILL_REVIEW_PROMPT
    agent._memory_store = None  # no live budget note on the memory path
    return agent


def _prompt(task_cfg, memory, skills):
    _t, prompt = spawn_background_review_thread(
        _bare_agent(), [], review_memory=memory, review_skills=skills, task_cfg=task_cfg)
    return prompt


def test_flag_unset_prompts_are_upstream_constants():
    for cfg in ({}, {"require_evidence": False}):
        assert _prompt(cfg, False, True) == _SKILL_REVIEW_PROMPT
        assert _prompt(cfg, True, True) == _COMBINED_REVIEW_PROMPT


def test_skill_only_prompt_gated():
    prompt = _prompt({"require_evidence": True}, False, True)
    assert _EVIDENCE_GATE in prompt
    assert "Be ACTIVE" not in prompt
    assert "should NOT be the default" not in prompt
    assert "Otherwise, act." not in prompt


def test_combined_prompt_gated():
    prompt = _prompt({"require_evidence": True}, True, True)
    assert _EVIDENCE_GATE in prompt
    assert "Be ACTIVE" not in prompt
    assert "don't reach for that conclusion as a default" not in prompt
    assert prompt.startswith("Review the conversation above and update two things")


def test_memory_only_prompt_unchanged_when_gated():
    gated = _prompt({"require_evidence": True}, True, False)
    plain = _prompt({}, True, False)
    assert gated == plain
    assert gated.startswith(_MEMORY_REVIEW_PROMPT)
    assert _EVIDENCE_GATE not in gated


def test_gate_keeps_lesson_layer_block():
    from agent.background_review import _LESSON_LAYER_BLOCK
    assert _LESSON_LAYER_BLOCK in _prompt({"require_evidence": True}, False, True)
    assert _LESSON_LAYER_BLOCK in _prompt({"require_evidence": True}, True, True)


def test_gate_does_not_touch_constants():
    _prompt({"require_evidence": True}, True, True)
    assert "Be ACTIVE" in _SKILL_REVIEW_PROMPT and "Be ACTIVE" in _COMBINED_REVIEW_PROMPT
