"""A card that only relays a comment onto another card never needs a hub slot. [daveey]

Workers refused ``foreign_card`` (or ``board_pinned``) by ``kanban_comment`` on a hub-owned card filed relay cards
pinned ``machine:hub`` ("only the hub can post them") that queued in the hub lane behind real hub work just to post
one comment (hub-pin sweep prevention slug ``foreign-comment-relay``: t_01a1cdddf74bf702, t_01a1cf3e8deb41b4,
t_01a1cf4778d6d1b1, t_01a1cf4799e84072). Any worker posts that comment itself: the operator call
(``kanban_supervisor_request(ops=[{"op": "operator", "action": "comment", ...}])``) or, on a
``capability:hub-write`` worker, ``hub-comment <card> --text|--file [--board B]``.

:func:`verdict` is the hub admission judge's deterministic first rule: such a card is denied with that route and no
model call, so the refusal the creating worker reads says what to do instead. A card that also names one of the
admission allowlist's contexts (hub Chrome, user terminal, a hub worktree, the hub
gateway's own key for ``hermes-hub`` (gateway_key_cli), in-process hub state (hub_live_process)) is left to the judge,
and so is a card that names no explicit target card (``on t_<id>``, a ``## t_<id>`` heading, ``task_id=t_<id>``).
"""
from __future__ import annotations

import re
from typing import Optional

# The title opens with a write of something onto a card ("Post two correction comments on ...").
_VERB = re.compile(r"^\W*(?:post|relay|leave|add|write|deliver|forward|copy)\b", re.IGNORECASE)
_PAYLOAD = re.compile(r"\b(?:comments?|notes?|evidence|results?|receipts?|corrections?)\b", re.IGNORECASE)
_COMMENT = re.compile(r"\b(?:comments?|kanban_comment|hub-comment)\b", re.IGNORECASE)
# The comment has an explicit target card: "on|onto|to [the] [hub-owned] [task|card] t_<id>" (markdown and a
# board/ prefix allowed), a "## t_<id>" section heading, `hub-comment t_<id>` or `task_id=t_<id>`.
_TARGET = re.compile(r"\b(?:on|onto|to)\s+(?:the\s+)?(?:hub-owned\s+)?(?:(?:task|card)\s+)?[*`]*(?:[\w-]+/)?"
                     r"t_[0-9a-f]{16}\b|^\s*#+\s*[*`]*t_[0-9a-f]{16}\b|hub-comment\s+t_[0-9a-f]{16}\b|"
                     r"task_id\s*=\s*['\"]?t_[0-9a-f]{16}\b", re.IGNORECASE | re.MULTILINE)
# What makes it a relay rather than code that happens to touch comments.
_RELAY = re.compile(r"verbatim|hub-owned|foreign_card|board_pinned|read_only_transport|replica|only the hub|"
                    r"owner-only|created on (?:the )?hub", re.IGNORECASE)
# The hub-admission-test allowlist's contexts: any mention goes to the judge instead.
_HUB_CONTEXT = re.compile(r"chrome|cloudflare access|run_in_user_terminal|"
                          r"user terminal|_wt/|\.worktrees/|unpushed|"
                          # gateway_key_cli: `hermes-hub <subcommand>` with the hub gateway's own key.
                          r"gateway_key_cli|hermes-hub\b|(?:gateway|hub)(?:'s|s)? (?:own )?key|"
                          r"hub-admin (?:cannot|can't|can not|is insufficient)|"
                          # hub_live_process: in-process hub gateway/serve state.
                          r"hub_live_process|in-process|live process|"
                          # The hub_reason schema's wording and the remaining literal allowlist IDs.
                          r"desktop[- ]session|signed-in browser|"
                          r"hub_chrome|user_terminal|hub_worktree", re.IGNORECASE)

WHY = ("comment relay: a card that only posts a comment on another card never needs the hub. Post it from your "
       "own card: kanban_supervisor_request(ops=[{'op': 'operator', 'action': 'comment', 'task_id': <card>, "
       "'text': ...}]) (its home gateway applies it), or on a capability:hub-write worker run "
       "`hub-comment <card> --text '...'` (or `--file <path>`, `--board <slug>` for another board). Do not file a "
       "relay card.")


def is_relay(title: str, body: str, hub_reason: Optional[str] = None) -> bool:
    """True when the card's only work is posting comment text onto other named cards."""
    title, body, reason = title or "", body or "", hub_reason or ""
    text = f"{title}\n{body}"
    return bool(_VERB.search(title) and _PAYLOAD.search(title) and _COMMENT.search(text) and _TARGET.search(text)
                and _RELAY.search(f"{text}\n{reason}") and not _HUB_CONTEXT.search(f"{text}\n{reason}"))


def verdict(fields: dict, hub_reason: Optional[str] = None):
    """The deny :class:`~hermes_cli.kanban_hub_admission.Verdict` for a comment-relay card, else None."""
    from hermes_cli.kanban_hub_admission import Verdict
    title = str(fields.get("title") or "")
    if not is_relay(title, str(fields.get("body") or ""), hub_reason):
        return None
    return Verdict(admit=False, need=None, evidence=title, why=WHY, model="rule:comment-relay")
