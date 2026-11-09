"""Text-response stop gates for the conversation turn loop.

When the model stops with a text answer, four gates may instead append the answer as an
interim row plus a user-role nudge and continue the turn: late language-server verdicts on this
turn's edits (a durable row, see ``_late_lsp_verdicts``), verify-on-stop (#65919),
the ``pre_verify`` plugin hook after code edits, and the kanban worker terminal-tool guard.
A fifth, the ``pre_final_gate`` hook [daveey], holds the answer silently (never emitted or
persisted) or appends to it; see ``agent/pre_final_gate.py``.
Each keeps the candidate answer as a budget-exhaustion fallback
(``pending_verification_response``) and clears ``final_response`` so the finalizer can tell
this gate from error exits (#61631). Nothing here imports ``agent.conversation_loop`` at
module level (cycle).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent.message_metadata import append_message
from agent.pre_final_gate import UnresolvedFallback

logger = logging.getLogger("agent.conversation_loop")

# Shown while ``pre_verify`` hooks judge an answer that has already streamed [daveey].
PRE_VERIFY_STATUS = "Checking the reply before finishing…"


@dataclass
class StopGateVerdict:
    """``continue_turn`` True → a nudge was appended; re-enter the turn loop with
    ``final_response=None`` and the pending-verification fields updated."""

    continue_turn: bool
    final_response: Any
    pending_verification_response: Any
    pending_verification_response_previewed: Any
    boundary: str = ""
    stream_deferred: bool = False

    def exit_reason(self, finish_reason: Any) -> str:
        if self.boundary:
            return f"pre_final_gate_unresolved({self.boundary})"
        return f"text_response(finish_reason={finish_reason})"


def _verify_on_stop_nudge(agent) -> Optional[str]:
    try:
        from agent.verification_stop import (
            build_verify_on_stop_nudge, verify_on_stop_enabled
        )

        if verify_on_stop_enabled():
            return build_verify_on_stop_nudge(
                session_id=getattr(agent, "session_id", None),
                changed_paths=getattr(agent, "_turn_file_mutation_paths", set()),
                attempts=getattr(agent, "_verification_stop_nudges", 0),
            )
    except Exception:
        logger.debug("verification stop-loop check failed", exc_info=True)
    return None


def _pre_verify_nudge(agent, final_response, attempt: int) -> Optional[str]:
    """After code edits (or on every text stop with ``agent.pre_verify_every_turn``) a registered
    ``pre_verify`` hook may keep the agent going one more turn; no default continuation cost.

    Detached forks (background review, side questions: ``_turn_origin`` / ``_persist_disabled``) share the
    parent's session_id but their text never reaches the user, so the hook is not consulted for them,
    matching ``post_llm_call`` in turn_finalizer. Otherwise a reply check judges the fork's summary
    against the user's last question and spends a continuation inside the fork."""
    _origin = getattr(agent, "_turn_origin", None)
    if _origin or getattr(agent, "_persist_disabled", False):
        logger.info("pre_verify skipped for fork turn origin=%s session=%s",
                    _origin or "persist_disabled", getattr(agent, "session_id", None) or "")
        return None
    _edited = sorted(getattr(agent, "_turn_file_mutation_paths", set()) or [])
    try:
        from agent.verify_hooks import max_verify_nudges, pre_verify_every_turn
        from hermes_cli.lifecycle import has_hook
        from hermes_cli.plugins import get_pre_verify_continue_message

        if (has_hook("pre_verify") and attempt < max_verify_nudges()
                and (_edited or pre_verify_every_turn())):
            # Posture is fixed for the session — resolve once + cache.
            coding = getattr(agent, "_resolved_is_coding", None)
            if coding is None:
                from agent.coding_context import is_coding_context
                coding = bool(is_coding_context(platform=getattr(agent, "platform", "") or ""))
                agent._resolved_is_coding = coding
            # The answer has already streamed; a reply-check hook can take tens of seconds (LLM judges), so
            # say so instead of leaving a bare spinner under a finished-looking answer (agent-7aa110cfd7).
            _sid = getattr(agent, "session_id", None) or ""
            try:
                agent._emit_status(PRE_VERIFY_STATUS)
            except Exception:
                logger.debug("pre_verify status emit failed", exc_info=True)
            _t0 = time.monotonic()
            _msg = None
            try:
                _msg = get_pre_verify_continue_message(
                    session_id=_sid,
                    platform=getattr(agent, "platform", "") or "",
                    model=getattr(agent, "model", "") or "", coding=coding, attempt=attempt,
                    final_response=final_response, changed_paths=_edited,
                )
                return _msg
            finally:
                logger.info("pre_verify done session=%s seconds=%.2f continued=%s attempt=%d",
                            _sid, time.monotonic() - _t0, bool(_msg), attempt)
    except Exception:
        logger.debug("pre_verify hook check failed", exc_info=True)
    return None


def _kanban_stop_nudge(agent, messages) -> Optional[str]:
    """Workers must end with a terminal board tool (kanban_complete / kanban_block /
    kanban_request_review / kanban_request_changes); a narrated stop is recorded
    as protocol_violation, so nudge once or twice first."""
    try:
        from agent.kanban_stop import build_kanban_stop_nudge

        return build_kanban_stop_nudge(
            messages=messages, attempts=getattr(agent, "_kanban_stop_nudges", 0)
        )
    except Exception:
        logger.debug("kanban stop-loop check failed", exc_info=True)
        return None


_MAX_LSP_TURN_END_NOTES = 3


def _late_lsp_verdicts(agent) -> Optional[str]:
    """Language-server verdicts on this turn's edits that no tool result carried yet (the edit was the
    last tool call; a one-shot run has no next turn to carry them): waits briefly for checks still
    running (``agent.lsp.deferred.settle_for_agent``) and returns them as a message for the model."""
    issued = getattr(agent, "_lsp_turn_end_notes", 0)
    if not isinstance(issued, int) or issued >= _MAX_LSP_TURN_END_NOTES:
        return None
    try:
        from agent.lsp.deferred import settle_for_agent

        notes = settle_for_agent(agent)
    except Exception:
        logger.debug("late LSP verdict check failed", exc_info=True)
        return None
    if not notes:
        return None
    body = "\n\n".join(text for _path, text in notes)
    return ("[System note: the language server reported problems in files you edited this turn, after "
            "those tool results had returned. Fix them if they are real, then give your final answer.]\n\n"
            + body)


def _append_interim_answer(agent, final_msg, messages, conversation_history, flush_fail_msg: str) -> bool:
    """Keep buffered candidates private until final output transformation. Returns whether the
    answer reached the screen: False when a final-gate buffer hid it, so the caller's nudge must
    say so (``held_nudge``) exactly like a pre_final_gate hold (agent-9382d717aa).

    Unbuffered legacy responses still emit and persist as verification interims: real content
    is persisted and emitted as interim so the user sees the attempted answer; only the nudge is
    flagged synthetic (#65919). A held turn's answer (typed hidden) is not shown and flushes with
    the turn instead, so a budget fallback can still publish it as the rewrite.
    """
    if getattr(agent, "_final_gate_buffer", None) is not None:
        # Output transforms have not reconciled this candidate yet. Retain it
        # only as ephemeral model context, exactly like a held gate candidate.
        # The budget fallback still traverses final output transformation.
        agent._discard_final_gate_buffer()
        final_msg[_PFG_FLAG] = True
        append_message(messages, final_msg)
        return False
    agent._emit_interim_assistant_message(final_msg)
    append_message(messages, final_msg)
    if final_msg.get("display_kind") == "hidden":
        return True
    try:
        agent._flush_messages_to_session_db(messages, conversation_history)
    except Exception:
        logger.debug(flush_fail_msg, exc_info=True)
    return True


# Safety net for a pre_verify continue that hid its draft (agent-9382d717aa): pre_verify asks for more work, never a
# retraction, so a revision under RESTORE_MAX_RATIO of a hidden draft of RESTORE_MIN_CHARS+ is a delta written as if
# the draft had been delivered. The draft already passed pre_final_gate (buffered turns run that gate first).
RESTORE_MIN_CHARS = 800
RESTORE_MAX_RATIO = 0.4


def _remember_hidden_draft(agent, final_response) -> None:
    if isinstance(final_response, str) and len(final_response.strip()) > len(
            getattr(agent, "_pre_verify_hidden_draft", None) or ""):
        agent._pre_verify_hidden_draft = final_response.strip()


def _restore_hidden_draft(agent, final_msg, final_response, *, deliver: bool):
    """Prefix the hidden pre_verify draft to a stub final reply; returns the (possibly) new reply."""
    draft = getattr(agent, "_pre_verify_hidden_draft", None) or ""
    agent._pre_verify_hidden_draft = None
    if (not isinstance(final_response, str) or len(draft) < RESTORE_MIN_CHARS
            or len(final_response.strip()) >= RESTORE_MAX_RATIO * len(draft) or draft in final_response):
        return final_response
    prefix = draft + "\n\n"
    restored = prefix + final_response.lstrip()
    key = "api_content" if final_msg.get("api_content") and not final_msg.get("content") else "content"
    final_msg[key] = restored
    if deliver:
        agent._deliver_stream_text(prefix)
    logger.info("restored_hidden_draft hook=pre_verify draft_chars=%d reply_chars=%d session=%s",
                len(draft), len(final_response), getattr(agent, "session_id", None) or "")
    return restored


def apply_stop_gates(
    agent: Any, final_msg: dict[str, Any], *, final_response: Any, messages: list[dict[str, Any]],
    conversation_history: Any, pending_verification_response: Any,
    pending_verification_response_previewed: Any,
    defer_stream: bool = False,
) -> StopGateVerdict:
    """Check buffered candidates before verify-on-stop → pre_verify → kanban. Nudges
    are user-role rows appended only after the assistant answer row, so role alternation
    holds. Hook lookups are imported lazily from their origin modules (tests patch them
    there)."""

    def _continue(nudge: str, flag: str) -> StopGateVerdict:
        """Append the synthetic nudge row and hand the turn back to the loop."""
        unresolved = isinstance(pending_verification_response, UnresolvedFallback)
        append_message(messages, {"role": "user", "content": nudge, flag: True})
        agent._session_messages = messages
        # Keep the answer only as a budget-exhaustion fallback; clear ``final_response`` so
        # the finalizer can tell this gate from error exits. Mark previewed only if the
        # candidate is reused (#61631).
        return StopGateVerdict(
            continue_turn=True, final_response=None,
            pending_verification_response=pending_verification_response if unresolved else final_response,
            # Prefix semantics on purpose: the streamed prefix IS on screen here. The gateway
            # interim verdict uses _interim_content_fully_streamed instead (#88954).
            pending_verification_response_previewed=not unresolved and agent._interim_content_was_streamed(
                final_response or ""
            ),
        )

    # Classify private candidates before verification can emit them as interims.
    # Unbuffered legacy plugins retain their existing gate order.
    from agent.pre_final_gate import needs_buffer

    early_gate = isinstance(pending_verification_response, UnresolvedFallback) or needs_buffer(agent)
    if not early_gate:
        verdict = _apply_verification_gates(agent, final_msg, final_response, messages,
                                          conversation_history, _continue)
        if verdict is not None:
            return verdict

    _decision = _pre_final_gate_decision(agent, final_response, messages, pending_verification_response)
    if _decision.recheck_accepted:
        pending_verification_response = None
    if _decision.replacement is not None:
        agent._discard_final_gate_buffer()
        final_response = _decision.replacement
        final_msg.clear()
        final_msg.update(role="assistant", content=final_response)
        pending_verification_response = None
        pending_verification_response_previewed = False
    if _decision.hold or (
        _decision.unresolved_fallback is not None and getattr(agent, "_interrupt_requested", False)
    ):
        # Silent hold [daveey]: no interim emit, no flush, buffered deltas dropped. The candidate
        # survives only as the budget-exhaustion fallback; both rows are ephemeral scaffolding.
        # hold=True is bounded: every hold increments _pre_final_gate_attempts, which
        # max_attempts() caps, so a plugin cannot hold a turn indefinitely.
        agent._pre_final_gate_attempts = getattr(agent, "_pre_final_gate_attempts", 0) + 1
        # Read before the discard: a buffered draft never streamed, so the model is told it was not shown
        # (an interrupt-only stop is not a hold and gets no notice).
        from agent.pre_final_gate import held_nudge

        shown = not _decision.hold or (
            isinstance(final_response, str) and agent._interim_content_was_streamed(final_response))
        agent._discard_final_gate_buffer()
        final_msg["finish_reason"] = "pre_final_gate_hold"
        final_msg[_PFG_FLAG] = True
        append_message(messages, final_msg)
        verdict = _continue(held_nudge(_decision.nudge or "Turn interrupted.", shown), _PFG_FLAG)
        if _decision.unresolved_fallback is not None:
            verdict.pending_verification_response = _decision.unresolved_fallback
            verdict.pending_verification_response_previewed = False
        logger.info("pre_final_gate hold issued (attempt %d)", agent._pre_final_gate_attempts)
        return verdict
    if _decision.unresolved_fallback is not None:
        agent._discard_final_gate_buffer()
        final_response = _decision.unresolved_fallback.render(_decision.boundary)
        final_msg.clear()
        final_msg.update(role="assistant", content=final_response)
        return StopGateVerdict(False, final_response, None, False, _decision.boundary)
    if early_gate:
        # Acceptance clears the old rejection before another gate can nudge.
        if isinstance(pending_verification_response, UnresolvedFallback):
            pending_verification_response = None
        verdict = _apply_verification_gates(agent, final_msg, final_response, messages,
                                          conversation_history, _continue)
        if verdict is not None:
            return verdict
    stream_deferred = defer_stream and getattr(agent, "_final_gate_buffer", None) is not None
    if _decision.replacement is not None:
        agent._pre_verify_hidden_draft = None  # a replacement retracts: never resurrect the hidden text
    else:
        final_response = _restore_hidden_draft(agent, final_msg, final_response, deliver=not stream_deferred)
    if _decision.replacement is not None and not stream_deferred:
        agent._deliver_stream_text(final_response)
    elif _decision.append:
        final_response = _append_to_final(
            agent, final_msg, final_response, _decision.append, deliver=not stream_deferred,
        )
    elif not stream_deferred:
        agent._flush_final_gate_buffer()
    return StopGateVerdict(
        continue_turn=False, final_response=final_response,
        pending_verification_response=(
            None if isinstance(pending_verification_response, UnresolvedFallback)
            else pending_verification_response
        ),
        pending_verification_response_previewed=pending_verification_response_previewed,
        stream_deferred=stream_deferred,
    )


def _apply_verification_gates(agent, final_msg, final_response, messages, conversation_history, _continue):
    # Legacy verification precedence, after buffered candidate acceptance.
    from agent.pre_final_gate import held_nudge
    _lsp_note = _late_lsp_verdicts(agent)
    if _lsp_note:
        agent._lsp_turn_end_notes = getattr(agent, "_lsp_turn_end_notes", 0) + 1  # int: checked above
        final_msg["finish_reason"] = "verification_required"
        _shown = _append_interim_answer(
            agent, final_msg, messages, conversation_history, "late-LSP interim flush failed"
        )
        # A durable row (no scaffolding flag): it is never stripped, so no sent byte changes later.
        append_message(messages, {"role": "user", "content": held_nudge(_lsp_note, _shown)})
        agent._session_messages = messages
        logger.debug("late LSP verdicts delivered before the final answer (%d)", agent._lsp_turn_end_notes)
        return StopGateVerdict(
            continue_turn=True, final_response=None,
            pending_verification_response=final_response,
            pending_verification_response_previewed=agent._interim_content_was_streamed(final_response or ""),
        )

    _verify_nudge = _verify_on_stop_nudge(agent)
    if _verify_nudge:
        agent._verification_stop_nudges = getattr(agent, "_verification_stop_nudges", 0) + 1
        final_msg["finish_reason"] = "verification_required"
        _shown = _append_interim_answer(
            agent, final_msg, messages, conversation_history, "verify-on-stop interim flush failed"
        )
        verdict = _continue(held_nudge(_verify_nudge, _shown), "_verification_stop_synthetic")
        # Internal nudge: stay silent on the terminal, debug-log only.
        logger.debug("verification stop-loop nudge issued (attempt %d)", agent._verification_stop_nudges)
        return verdict

    _attempt = getattr(agent, "_pre_verify_nudges", 0)
    _verify_nudge2 = _pre_verify_nudge(agent, final_response, _attempt)
    if _verify_nudge2:
        agent._pre_verify_nudges = _attempt + 1
        final_msg["finish_reason"] = "verify_hook_continue"
        _shown = _append_interim_answer(
            agent, final_msg, messages, conversation_history, "pre_verify interim flush failed"
        )
        if not _shown:
            _remember_hidden_draft(agent, final_response)
        verdict = _continue(held_nudge(_verify_nudge2, _shown), "_pre_verify_synthetic")
        logger.debug("pre_verify nudge issued (attempt %d)", agent._pre_verify_nudges)
        return verdict

    _kanban_nudge = _kanban_stop_nudge(agent, messages)
    if _kanban_nudge:
        agent._kanban_stop_nudges = getattr(agent, "_kanban_stop_nudges", 0) + 1
        final_msg["finish_reason"] = "kanban_terminal_required"
        # Like verify-on-stop/pre_verify, only the nudge is scaffolding: the answer is a real model
        # response and must be durable before the next request (an observed managed worker binds
        # each SDK response to one durable assistant row and faults closed otherwise).
        _shown = _append_interim_answer(
            agent, final_msg, messages, conversation_history, "kanban stop interim flush failed"
        )
        verdict = _continue(held_nudge(_kanban_nudge, _shown), "_kanban_stop_synthetic")
        logger.info(
            "kanban stop-loop nudge issued (attempt %d) task=%s",
            agent._kanban_stop_nudges,
            os.environ.get("HERMES_KANBAN_TASK", ""),
        )
        agent._emit_diagnostic_status(
            "⚠️ Kanban worker tried to exit without a terminal board call "
            "(kanban_complete/kanban_request_review/kanban_block) — nudging to finish"
        )
        return verdict

    return None


_PFG_FLAG = "_pre_final_gate_synthetic"


def _pre_final_gate_decision(agent, final_response, messages, pending=None):
    from agent.pre_final_gate import GateDecision, _unavailable, evaluate

    decision = GateDecision()
    if not getattr(agent, "_interrupt_requested", False):
        try:
            decision = evaluate(agent, final_response, messages, getattr(agent, "_pre_final_gate_attempts", 0), pending)
        except Exception:
            logger.warning("pre_final_gate evaluation failed", exc_info=True)
            decision = _unavailable(pending)
    # Check after callback completion too: cancellation can arrive during a slow
    # callback or its failure path. Unvalidated buffered text must not escape.
    if (getattr(agent, "_interrupt_requested", False)
            and getattr(agent, "_final_gate_buffer", None) is not None):
        return GateDecision(hold=True, nudge="Turn interrupted.",
                            replacement="Response interrupted before validation.")
    return decision


def _append_to_final(agent, final_msg, final_response: str, extra: str, *, deliver: bool = True) -> str:
    """Deliver with ``extra`` appended: rewrite the row, release the buffer, stream the suffix so
    streamed text and the final answer still match."""
    suffix = "\n\n" + extra
    final_response = final_response.rstrip() + suffix
    key = "api_content" if final_msg.get("api_content") and not final_msg.get("content") else "content"
    if isinstance(final_msg.get(key), str):
        final_msg[key] = final_msg[key].rstrip() + suffix
    if deliver:
        agent._flush_final_gate_buffer()
        if getattr(agent, "_streamed_assistant_text_parts", None):  # only extend a live stream
            agent._deliver_stream_text(suffix)
    return final_response
