"""Stall-guard tail detectors: does a text stop END on an announced next action instead of an answer?

Split out of ``agent.agent_runtime_helpers`` (which re-exports the two older names). Callers:
``agent.turn_final_response.finish_text_response`` (the said-continue-but-stopped re-prompt).
"""

import re


# Narrow "trailing continue-intent" detector for the stall guard (agent.stall_guards): only the
# message TAIL announcing a next action, so mid-sentence "I will" never trips it.
_TRAILING_CONTINUE_INTENT_RE = re.compile(
    r"(?:\blet me now\b|\bi(?:['\u2019])?ll now\b|\bi will now\b"
    r"|\bnow i(?:['\u2019]ll| will)\b|\bnext[,:] i\b)"
    r"[^.!?\n]{0,100}[.:\u2026]?\s*$", re.IGNORECASE,
)

# Content longer than this is a substantive reply, not a dangling ack.
_TRAILING_CONTINUE_INTENT_MAX_CHARS = 400


def trailing_continue_intent(text: str) -> bool:
    """Whether ``text`` is a short reply ENDING on an announced next action (stall-guard re-prompt trigger)."""
    t = (text or "").strip()
    if not t or len(t) > _TRAILING_CONTINUE_INTENT_MAX_CHARS:
        return False
    return bool(_TRAILING_CONTINUE_INTENT_RE.search(t[-160:]))


def trailing_colon_handoff(text: str) -> bool:
    """Whether a short reply ENDS on ":" in prose — an announced next step, never a finished answer.

    "Reading the card first, together with the PR:" or "Checking the PR now:" with no tool call is a
    stalled model (defect agent-b6540f8eef): the narrow phrase list above misses every plain
    wording, but the dangling colon is the signal itself. A colon inside an unclosed code fence is
    code (``key:``), not a hand-off. The caller also requires zero tool calls this turn.
    """
    t = (text or "").rstrip()
    if not t or len(t) > _TRAILING_CONTINUE_INTENT_MAX_CHARS or not t.endswith(":"):
        return False
    return t.count("```") % 2 == 0


# Broader tail detector for PROMOTED REASONING only (reasoning-only clean stop with tools offered
# and no tool call). Visible content keeps the narrow ``let me now`` shape above because a real
# reply legitimately says "I'll" mid-text; chain-of-thought that ENDS on a first-person plan
# ("Let me batch the terminal calls and run them in parallel.", "I need to check the log.") is a
# stalled model whose turn would otherwise report "complete" with zero tool calls (#111761).
# Tail-only and anchored on the last sentence, so reasoning that merely mentions a plan before
# stating its answer ("...Let me check. The answer is 42.") still promotes.
# Thai (unsegmented script, so no \b after the trigger, unlike the English group) shares the same
# tail shape: a first-person future-action marker immediately followed by more Thai text, often
# preceded by an em/en dash rather than sentence punctuation (#116495). Trigger glosses, in
# pattern order: "I will give you" / "I will", "next I('ll)" + one of {start,try,check,fix,send,
# do,look}, "please let me" + one of {start,try,check,fix,send,do,look}, "I('ll)" + one of
# {start,try,check,fix,send,do,look,run,fire}.
_PROMOTED_REASONING_PLAN_TAIL_RE = re.compile(
    r"(?:^|[.!?:\u3002\uff01\uff1f\u2014\u2013\n]\s*|\u2026\s*)"
    r"(?:let(?:['\u2019]s| me)\b|i(?:['\u2019]ll| will| need to| should| am going to|['\u2019]m going to)\b"
    r"|next[,:]? i\b|now i(?:['\u2019]ll| will| need to)\b|first[,:]? i(?:['\u2019]ll| will| need to)\b"
    r"|\u0e08\u0e30\u0e43\u0e2b\u0e49\u0e1c\u0e21|\u0e1c\u0e21\u0e08\u0e30"
    r"|\u0e15\u0e48\u0e2d\u0e44\u0e1b(?:\u0e08\u0e30|\u0e1c\u0e21\u0e08\u0e30)"
    r"|\u0e02\u0e2d(?:\u0e40\u0e23\u0e34\u0e48\u0e21|\u0e25\u0e2d\u0e07|\u0e15\u0e23\u0e27\u0e08|\u0e41\u0e01\u0e49|\u0e2a\u0e48\u0e07|\u0e17\u0e33|\u0e14\u0e39)"
    r"|\u0e08\u0e30(?:\u0e40\u0e23\u0e34\u0e48\u0e21|\u0e25\u0e2d\u0e07|\u0e15\u0e23\u0e27\u0e08|\u0e41\u0e01\u0e49|\u0e2a\u0e48\u0e07|\u0e17\u0e33|\u0e14\u0e39|\u0e23\u0e31\u0e19|\u0e22\u0e34\u0e07))"
    r"[^.!?\n\u3002\uff01\uff1f]{0,160}(?:[.:\u2026]+)?\s*$",
    re.IGNORECASE,
)


def promoted_reasoning_announces_action(text: str) -> bool:
    """Whether promoted reasoning ENDS on a first-person plan to act (stall, not an answer).

    No overall length cap: the reasoning block of a stalled model is often 300-1600 chars of
    planning monologue; only the tail decides.
    """
    t = (text or "").strip()
    if not t:
        return False
    return bool(_PROMOTED_REASONING_PLAN_TAIL_RE.search(t[-240:]))
