"""Block is never a terminal parking state for agent-owned reasons.

``capability`` / ``transient`` / ``needs_input`` (without a human_request) / untyped blocks with no human ask, no wake
probe and no routed assignee have no owner: nothing wakes them. Worker-facing
entry points (tool + CLI) refuse them with a typed error naming the fix; the
dispatcher re-spawns any that already exist (``sweep_agent_owned_blocks``);
creation refuses ``blocked`` cards that carry no human ask.
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Optional

# needs_input is agent-owned when it carries no human_request: the sidebar only shows a block as
# blocked when needs_human() holds, so an ask-less needs_input looks like work in progress.
AGENT_OWNED_KINDS = (None, "capability", "transient", "needs_input")
AGENT_BLOCK_GRACE_SECONDS = 2 * 3600
ERROR_CODE = "agent_owned_block"
_DECISION_TITLE = re.compile(r"^\s*(for you\s*:|choose\b|decide\b|approve\b)", re.IGNORECASE)
# Copyable shape for the ask, appended to every refusal: agents that hit the guard retried by guessing
# (reason="placeholder" probes, a tool_describe round trip) or moved the ask into reason/body again. [daveey]
HUMAN_REQUEST_SKELETON = (
    "human_request={'action': '<the exact step for David and where>. Unblock = <what happens next>; "
    "archive = <what is dropped>', 'why_human': '<why no agent can do it>', 'interaction': "
    "'approval|reply|user_terminal|physical|external', 'scope': '<what it covers>', 'evidence': "
    "'<what you checked>'}"
)
_SKELETON_NOTE = (" A real ask for David goes in human_request, not in reason or body; copy and fill: "
                  + HUMAN_REQUEST_SKELETON + ".")


def block_policy_error(kind, *, human_request=None, wake_check=None, clock_only=False,
                       blocker_id=None, reason=None, exhausted_budget=False, task=None) -> Optional[str]:
    """Typed refusal text for an ownerless block, else None.

    ``exhausted_budget`` (or ``task=(conn, task_id)`` to compute it) is the DB-verified
    :func:`hermes_cli.kanban_continuation_recovery.is_exhausted_budget_block`: that block is the one
    ``kanban_continue`` prescribes after its refusal, and ``block_task`` routes it to agent triage (an
    owner), so it is not ownerless. Before this the guard refused the prescribed call (8 sessions). [daveey]
    """
    if kind not in AGENT_OWNED_KINDS:
        return None
    if task is not None and not exhausted_budget:
        from hermes_cli.kanban_continuation_recovery import is_exhausted_budget_block
        exhausted_budget = is_exhausted_budget_block(task[0], task[1], kind, reason)
    if human_request or wake_check or clock_only or blocker_id or exhausted_budget:
        return None
    if reason and "SCHEDULED_UNTIL=" in reason:
        return None
    from hermes_cli.kanban_assignment_routing import NO_SECOND_PROFILE_ROUTES
    if kind == "needs_input":
        return (
            f"{ERROR_CODE}: kind='needs_input' with no human_request is invisible to David (the "
            "sidebar shows it as active) and nothing wakes it. Pick one: (1) a step only David can "
            "take -> pass human_request (action, why_human, interaction, scope, evidence); (2) a "
            "file, permission or tool another gateway holds -> declare the capability need "
            "(needs=['host:<gateway>'] or the exact capability, e.g. 'ssh:mini') so an owner card "
            "is opened on that gateway, or kanban_create the owner card there and park on it "
            "(kind='time', awaiting={kind:'card', ref, gateway}); (3) a timed wait -> kind='time' "
            "with awaiting plus wake_check (or clock_only=true); (4) archive, unlink, resume or "
            "retry_limit on a card in your creator tree -> kanban_supervisor_request."
        ) + _SKELETON_NOTE
    if _assignment_routes(task):
        agent_work = (
            "create the owner card on the gateway that holds the capability and park on it (kind='time', "
            "awaiting={kind:'card', ref, gateway}), or kind='assignment' so a capable supervisor is routed "
            "(a different profile is configured for this card)")
    else:
        agent_work = "use one of: " + NO_SECOND_PROFILE_ROUTES.rstrip(".")
    return (
        f"{ERROR_CODE}: kind={kind!r} with no human_request, wake_check or clock_only leaves the "
        "card with no owner and nothing to wake it. Pick one: (1) a decision/action only David can "
        "take -> pass human_request (kind needs_input or capability); (2) a condition an agent action "
        f"changes (a grant, a permission, an install, a fix) is work, not a wait -> {agent_work}; "
        "(3) a wait needs a producer -> kind='time' with awaiting naming it (a live card, a "
        "CI run, a vendor ticket, a real clock window) plus wake_check (or clock_only=true). "
        "If the work is actually done, call kanban_complete; if it needs another card, "
        "kanban_create it and link it as a parent."
    ) + _SKELETON_NOTE


def _assignment_routes(task) -> bool:
    """kind='assignment' can route the card in ``task=(conn, task_id)``; False without one or on any error.

    Single-profile hosts (no kanban.orchestrator_profile, creator == assignee) always refuse it with
    no_capable_supervisor, and offering it there sent 38 cards in 72 h into that wall. [daveey]
    """
    if task is None:
        return False
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_assignment_routing import supervisor_available
        row = kb.get_task(task[0], task[1])
        return row is not None and supervisor_available(row)
    except (sqlite3.Error, ValueError, OSError):  # an unreadable card or profile never offers a route that may fail
        return False


# Noise filter only, not an authorization classifier: no regex can tell which asks need David (PR1220
# reviews missed 17/17, 39/52, then 30/38). The safety comes from RECOMMENDATION_WARNING being
# conditional; these patterns just keep it off asks with no code signal or an obvious boundary. [daveey]
_RECOMMENDATION_IN_GATE = re.compile(
    r"\b(?:prs?|pull[ -]requests?|commits?|tests?|testing|unit[ -]tests?|fixtures?|modules?|helpers?|shims?|"
    r"renam\w*|refactor\w*|timeouts?|retr(?:y|ies)|backoff|configs?|configuration|(?:feature[ -])?flags?|"
    r"pre-?merge|post-?merge|lint\w*|port\s+\d{2,5}|worktrees?|functions?|code|docs?|docstrings?|schemas?|"
    r"regex\w*|parsers?|poller|branch(?!\s+protection)|review[ -](?:tier|gate)|code[ -]review)\b"
    r"|:\d{2,5}\b|\btests?/\S+|\b[\w/-]+\.py\b", re.IGNORECASE)
_RECOMMENDATION_BOUNDARY = re.compile(
    # money
    r"\b(?:spend\w*|cost\w*|budget\w*|pay\w*|purchas\w*|money|dollars?|bill\w*|charg\w*|rent\w*|"
    r"subscri\w*|pric\w*|fees?|invoic\w*|refund\w*|paid|buy\w*|bought|order\w*|book\w*|regist\w*|renew\w*|"
    r"donat\w*|cancel\w*|sell\w*|sold|transfer\w*|wire|plans?|upgrad\w*|downgrad\w*|reserv\w*|"
    r"terms|agreement\w*|agree\w*|accept(?!ance)\w*|sign|signs|signed|signing|contract\w*|"
    # infrastructure, production and spend-bearing compute
    r"prod|production|deploy\w*|releas\w*|ship|shipping|hotfix\w*|terminat\w*|tear\s+down|teardown|"
    r"decommission\w*|shut\s*down|restor\w*|snapshot\w*|backups?|migrat\w*|rollback|kill\w*|restart\w*|"
    r"reboot\w*|power\w*|enabl\w*|disabl\w*|turn\s+(?:on|off|up|down)|install\w*|branch\s+protection|"
    r"visib\w*|rewrite\s+\w*\s*history|history\s+rewrite|merge\s+(?:\w+\s+){0,3}without|"
    r"spin\w*\s+up|gpus?|[ahb]\d00s?|instances?|clusters?|servers?|vms?|ec2|aws|gcp|lambda|domains?|dns|"
    r"certs?|certificates?|hub|mini|router|bridge|hue|lights?|locks?|unlock\w*|alarm\w*|speakers?|eo1|"
    r"doors?|garage|cameras?|thermostat\w*|monitor|nursery|"
    r"archiv\w*\s+(?:the|a|an|this|that|these|those|all|old|our|my|every|it)\b|"
    r"post(?:ed|ing|s)?\s+(?:on|to|the|a|it|this|publicly)\b|submit\w*|"
    r"share\w*\s+(?:with|the|it|this|that|a|an|my|our|publicly)\b|"
    r"dms?|recruit\w*|partner\w*|"
    # permissions and credentials
    r"permission\w*|credential\w*|password\w*|tokens?|secrets?|access|(?:api|ssh|signing|private|deploy)[ -]keys?|"
    r"log[ -]?in\w*|log[ -]?out|sign[ -]?(?:in|out|up)|auth\w*|oauth|accounts?|revok\w*|rotat\w*|grant\w*|admin\w*|"
    # safety, privacy, consent
    r"consent|privacy|private|safety|health|medical|"
    # destructive or hard to reverse
    r"delet\w*|remov\w*|wip\w*|purg\w*|eras\w*|destroy\w*|truncat\w*|overwrit\w*|reset\w*|"
    r"force[- ]?push\w*|irreversibl\w*|destructive|permanent\w*|unrecoverabl\w*|hard[- ]to[- ]reverse|"
    r"bypass\w*|without\s+(?:ci|review|tests?|checks?|approval)|skip\w*\s+(?:ci|review|tests?|checks?)|"
    r"(?:failing|failed|red|broken)\s+(?:required\s+)?(?:ci|checks?|builds?)|"
    # other people and outward communication
    r"person|people|someone|anyone|Rose|David's|wife|husband|partner|family|friend\w*|landlord|neighbou?r\w*|"
    r"colleague\w*|team\w*|reviewer\w*|customer\w*|client\w*|user\w*|vendor\w*|contractor\w*|recipient\w*|"
    r"email\w*|e-mail\w*|send\w*|sent|publish\w*|public\w*|message\w*|tweet\w*|"
    r"(?:on|to)\s+(?:x|twitter|bluesky|slack|discord|linkedin|social\w*)|"
    r"announc\w*|invit\w*|notify\w*|"
    # physical
    r"physical|press|plug|unplug|touch)\b|[$€£]", re.IGNORECASE)
# "Ask Alice to review", "Bob's PR", "@sam", "share with Ana", "ask david": a named person. [daveey]
_RECOMMENDATION_NAMED_PERSON = re.compile(
    r"\b(?:[Aa]sk|[Tt]ell|[Pp]ing|[Cc]ontact|[Cc]all|[Tt]ext|[Cc]c)\s+[A-Z][a-z]+\b|\b[A-Z][a-z]+'s\b|@\w+|"
    r"(?i:\b(?:ask|tell|ping|dm|email|message|invite|share\s+with|reply\s+to|cc)\s+"
    r"(?!(?:the|a|an|this|that|these|those|me|us|it|them|worker|agent|whether|if|about|for|to|on|in)\b)"
    r"[a-z]+\b)")


def recommended_choice_warning(human_request) -> dict:
    """Non-fatal nudge for a recommended decision; never applies it or changes admission."""
    if not isinstance(human_request, dict):
        return {}
    if human_request.get("interaction") not in {"reply", "approval"}:
        return {}
    choices = human_request.get("choices")
    if not isinstance(choices, list):
        return {}
    choices = [choice for choice in choices if isinstance(choice, dict)]
    # Explicit positive UI markers only; 'not recommended' is not a recommendation.
    recommended = [choice for choice in choices if re.search(
        r"\(recommended\)|\[recommended\]", str(choice.get("label") or ""), re.IGNORECASE)]
    if not recommended:
        return {}
    # Fail-safe: a positive in-gate signal in the action or the recommended pick itself. Scope alone is
    # worker-written boilerplate, so it never qualifies an ask on its own. [daveey]
    signal = str(human_request.get("action") or "") + " " + " ".join(
        str(choice.get(key) or "") for choice in recommended for key in ("label", "value"))
    if not _RECOMMENDATION_IN_GATE.search(signal):
        return {}
    text = " ".join(str(human_request.get(key) or "")
                    for key in ("action", "scope", "why_human", "evidence"))
    text += " " + " ".join(str(choice.get(key) or "") for choice in choices for key in ("label", "value"))
    # Nothing is stripped before the veto: every boundary word in any field is seen. The ask template's
    # own verbs ("reply", "drop") are simply not veto terms (review t_01a1cebce8757c5a). [daveey]
    if _RECOMMENDATION_BOUNDARY.search(text) or _RECOMMENDATION_NAMED_PERSON.search(text):
        return {}
    return {"warning": RECOMMENDATION_WARNING}


# Conditional and non-affirming: it never says this ask was in-gate, so a misfire on an ask that really
# needs David teaches nothing wrong (PR1220 review t_01a1cea8ae43e248). The block has landed and David
# is asked, so the advice is for this card's comment and for next time, never "apply it now". [daveey]
RECOMMENDATION_WARNING = (
    "You marked a recommended option. Under David's standing rule a decision is yours to apply, not "
    "ask, only when it is in scope, reversible, and within the existing review and landing gates, and "
    "crosses none of: spend beyond standing limits, permissions or credentials, other people or outward "
    "communication, safety or privacy consent, production systems or devices, hard-to-reverse effects, "
    "or a real toss-up. If any of these applies, ignore this note; it does not judge this ask and grants "
    "no permission. Otherwise: this block stands and this run still ends, so do not apply the option "
    "now; comment your recommended pick and how to reverse it so David can just unblock, and next time "
    "apply the recommended option and comment the pick instead of blocking.")
ASK_LINT_CODE = "user_ask_incomplete"
_UNBLOCK = re.compile(r"\bun-?block", re.IGNORECASE)
_ARCHIVE = re.compile(r"\barchiv", re.IGNORECASE)


RELAY_ASK_CODE = "agent_relay_ask"
# "from/in your own Hermes (Desktop|CLI) chat", "ask/tell your (Hermes) chat/agent/session to ...": the ask only
# makes David carry a message to an agent. Kept narrow: "asks the vendor's agent" or "sign in" never match.
_RELAY = re.compile(
    r"\b(?:from|in|via|using)\s+your\s+own\s+(?:hermes\s+)?(?:desktop\s+|cli\s+)?(?:or\s+cli\s+)?"
    r"(?:chat|session|agent)\b"
    r"|\b(?:ask|tell|have|get)\s+your\s+(?:own\s+)?(?:hermes\s+)?(?:desktop\s+|cli\s+)?"
    r"(?:chat|session|agent)\b"
    # "or from a Hermes chat there" (MacBook card t_01a18b13ee350599): the same relay through any chat. [daveey]
    r"|\b(?:from|in|via|using)\s+(?:a|an|any|the)\s+hermes\s+(?:desktop\s+|cli\s+)?(?:chat|session)\b"
    # "from an owner Hermes chat" (hub t_01a1848506cae619), "from any owner chat", "from an owner Hermes Desktop or
    # CLI chat": an owner chat is an agent the ask makes David relay to. [daveey]
    r"|\b(?:from|in|via|using)\s+(?:(?:a|an|any|the)\s+)?owner(?:'s)?\s+"
    r"(?:hermes\s+)?(?:desktop\s+|cli\s+)?(?:or\s+cli\s+)?"
    r"(?:chat|session)\b",
    re.IGNORECASE)

ROUTE_GAP_CODE = "agent_route_gap"
BOARD_OP_CODE = "board_op_ask"
# [daveey] self-heal agent-7489fcf5cd: "One board action is required: mark `t_01a1a521638d0a78` Done in Hermes
# Kanban". The ask line (before its Unblock/archive tail) is an imperative Kanban write on a named card: a
# bookkeeping step agents have routes for. Kept narrow: a card id, a board verb first in the clause, a board term.
_BOARD_OP = re.compile(
    r"(?:^|[.:;]\s*)(?:please\s+)?(?:mark|move|set|complete|close|archive|edit|change|re-?route|delete|cancel)\b"
    r"(?:\s+(?:card|task))?\s+`?t_[0-9a-f]{8,}`?"
    r"[^.;]{0,80}?\b(?:done|complete[d]?|archived?|runs_on|labels?|status|board|kanban|dashboard)\b",
    re.IGNORECASE)
_ASK_TAIL = re.compile(r"\bun-?block\s*=", re.IGNORECASE)


def board_op_ask_error(human_request) -> Optional[str]:
    """Typed refusal for a human_request whose ask is only a Kanban edit/complete/archive of a named card."""
    if not isinstance(human_request, dict):
        return None
    action = " ".join(str(human_request.get("action") or "").split())
    head = _ASK_TAIL.split(action, 1)[0]
    m = _BOARD_OP.search(head)
    if not m:
        return None
    return (f"{BOARD_OP_CODE}: human_request.action only asks David to edit, complete or archive a Kanban card "
            f"({m.group(0).strip(' .:;')!r}). Board bookkeeping is agent work: a ready card stranded on runs_on no "
            "machine carries -> kanban_move(task_id=<it>, runs_on=['machine:<registered-name>'], reason=...); a "
            "card another gateway created -> kanban_supervisor_request operator op; an unstarted orphan in your "
            "creator tree -> kanban_supervisor_request archive op; a card whose own run must finish it -> "
            "kanban_comment the proof on it so its worker completes it. Keep human_request for what only David "
            "can do: approve, pay, sign in, a physical step.")
# The typed answers of the agent routes (capability routing, placement, supervisor routing). A human_request that
# cites one of them as its reason asks David to do an agent's step because the route failed. [daveey]
_ROUTE_GAP = re.compile(
    r"\bno_capability_holder\b|\bcapability_registry_(?:unavailable|stale)\b|\bno_capable_supervisor\b"
    r"|\bplacement_unavailable\b|\bnot live\b(?=[^.;\n]{0,40}\bplacement)|\bplacement\b[^.;\n]{0,60}\bnot live\b"
    r"|\bno agent route\b|\bno gateway holds\b",
    re.IGNORECASE)
# ... and asks him to run the step and hand back its output: an agent's step, not a grant or a sign-in.
_RUN_AND_REPORT = re.compile(
    r"\b(?:paste|post|send|report|share|copy)\b[^.;\n]{0,40}\b(?:results?|output|logs?|pass/fail)\b",
    re.IGNORECASE)


def agent_route_gap_error(human_request) -> Optional[str]:
    """Typed refusal for a human_request that asks David to run a step and report back because an agent route failed.

    MacBook card t_01a18b13ee350599, run on worker-1, asked David to "run the 8 drain test files ... and paste the
    results", with why_human "No agent route to the MacBook ... no gateway holds host:macbook" and evidence
    "needs=['host:macbook'] -> no_capability_holder". A MacBook chat did the step in 9 minutes. A route failure is
    a routing bug for an agent to fix, never David's task.
    """
    if not isinstance(human_request, dict):
        return None
    text = " ".join(str(human_request.get(k) or "") for k in ("why_human", "evidence", "action"))
    m = _ROUTE_GAP.search(text)
    if not m or not _RUN_AND_REPORT.search(str(human_request.get("action") or "")):
        return None  # a grant only David can give (the documented no_capability_holder ask) stays allowed
    return (f"{ROUTE_GAP_CODE}: this human_request asks David to do a step because an agent route failed "
            f"({m.group(0)!r}). That is a routing gap, not his task. Instead: (1) retry kanban_block(needs=[...]) "
            "(a briefly stale holder now parks and retries; a Board worker node relays the need to the card's home "
            "gateway); (2) kanban_create the owner card on the holder's gateway or pin it with runs_on="
            "['machine:<holder>'] and park on it; (3) if routing is broken, kanban_create a card to fix it and park "
            "on that card. Keep human_request for what only David can do: approve, pay, sign in, a physical step.")


# Presentation lint only, never an execution grant. Use a closed template rather than infer
# authority from arbitrary prose. Details belong in scope/reason; runtime owner-only gates still apply.
_OWNER_TERMINAL_ACTION = re.compile(
    r"From your own Hermes chat (?:open|start) the owner-terminal flow for task t_[0-9a-f]{8}(?:[0-9a-f]{8})?\. "
    r"Unblock = resume verification; archive = abandon reconciliation\.", re.IGNORECASE)
_OWNER_TERMINAL_REASON = "host_only boundary: only the owner's Desktop chat may open a terminal"
_OWNER_TERMINAL_EVIDENCE = (
    "run_in_user_terminal refused: Kanban caller, owner chats only. "
    "cross_session send refused: Kanban worker cannot send into a human chat.")


def owner_only_action_evidenced(human_request) -> bool:
    """Accept only the canonical owner-terminal initiation attestation.

    This describes prior refusals; it does NOT authenticate receipts or authorize execution. A free-text
    'refused' keyword cannot distinguish generic relays, negations or hypothetical refusals, so callers
    must explicitly attest the complete template, with additional details in scope/reason. All actual
    terminal and cross-session owner-only checks remain authoritative and unchanged.
    """
    if not isinstance(human_request, dict):
        return False
    fields = {k: " ".join(str(human_request.get(k) or "").split())
              for k in ("interaction", "action", "why_human", "evidence")}
    return (fields["interaction"] == "user_terminal"
            and _OWNER_TERMINAL_ACTION.fullmatch(fields["action"]) is not None
            and fields["why_human"] == _OWNER_TERMINAL_REASON
            and fields["evidence"] == _OWNER_TERMINAL_EVIDENCE)


def agent_relay_ask_error(action: str, human_request=None) -> Optional[str]:
    """Typed refusal for an ask that only relays a message from David to an agent (self-heal agent-c03353a3cf).

    Hub t_01a188c8ddf998ac asked David to "ask for the broker Gemini secret repair from your own Hermes chat on
    the AWS-authorized Mac"; that chat did it itself in 10 minutes. Such a block is a routing gap shown to David
    as his task, so the worker must route it to an agent instead.
    """
    if not _RELAY.search(action) or owner_only_action_evidenced(human_request):
        return None
    return (f"{RELAY_ASK_CODE}: human_request.action asks David to pass a message to an agent or chat "
            "(\"from your own Hermes chat\", \"ask your agent to ...\"). Agents can do that without him: "
            "(1) declare the capability the other gateway holds with kanban_block(needs=['aws:<account>:<scope>' "
            "| 'ssh:<host>' | 'host:<gateway>']) so an owner card opens there; or (2) kanban_comment the exact "
            "request and send it to the owning chat with cross_session (action='send'), or kanban_create the "
            "owner card on that gateway (placement='<gateway>') and park on it. For the host_only owner-terminal "
            "boundary only, use interaction='user_terminal', action='From your own Hermes chat open the "
            "owner-terminal flow for task <task_id>. Unblock = resume verification; archive = abandon reconciliation.', "
            f"why_human={_OWNER_TERMINAL_REASON!r}, evidence={_OWNER_TERMINAL_EVIDENCE!r}; only attest actual "
            "refusals, keep details in scope/reason. This lint exception grants no execution authority. If no gateway holds the "
            "capability, the gap is the capability registry: fix the probe config (kanban.capabilities.probes) "
            "on the host that has the access. Keep human_request for what only David can do: approve, pay, "
            "sign in, a physical step.")


def user_ask_lint_error(human_request) -> Optional[str]:
    """Typed refusal when a block on David does not say, in its ask line, what unblock and archive mean.

    ``human_request.action`` is the one line every surface shows David verbatim (card view, For-you queue, chat
    close-out and ask-relay notices). An action like "Pick A or B" or "the safety change" left him reading
    "blocked" with no idea what to do or what his click would do (coach pattern blocked-card-missing-user-ask).
    An action that only asks him to relay a message to an agent is refused too (:func:`agent_relay_ask_error`).
    Only agent entry points (kanban_block / kanban_create tools) call this; internal trackers are not linted.
    """
    if not isinstance(human_request, dict):
        return None
    action = " ".join(str(human_request.get("action") or "").split())
    relay = (agent_relay_ask_error(action, human_request) or agent_route_gap_error(human_request)
             or board_op_ask_error(human_request))
    if relay:
        return relay
    missing = [label for label, rx in (("what unblocking the card does", _UNBLOCK),
                                       ("what archiving it does", _ARCHIVE)) if not rx.search(action)]
    if not missing:
        return None
    return (f"{ASK_LINT_CODE}: human_request.action is the ask line David sees verbatim on the card and in chat "
            f"notices, and it does not say {' or '.join(missing)}. Rewrite action as one self-contained line: "
            "the exact thing to do and where, then 'Unblock = <what happens next>; archive = <what happens / "
            "what is dropped>'. Example: \"Reply with the owner (david, household or system) for device "
            "'barley' in this chat. Unblock = the agent records it and enforces privacy; archive = leave it "
            "unowned.\"")


def ask_lint_check(human_request) -> tuple:
    """``_check`` args for the tool entry points: ``(ok, message, rejection)``."""
    err = user_ask_lint_error(human_request)
    code = next((c for c in (RELAY_ASK_CODE, ROUTE_GAP_CODE, BOARD_OP_CODE) if err and err.startswith(c)),
                ASK_LINT_CODE)
    return err is None, err or "", code


def creation_lint_error(title, initial_status, human_request, *, triage=False) -> Optional[str]:
    """Refuse decision cards / blocked cards that carry no human ask."""
    if human_request:
        return None
    if initial_status == "blocked" and not triage:
        return (f"{ERROR_CODE}: initial_status='blocked' requires human_request (the ask for David). "
                "If no human step is needed, omit initial_status: an agent wait on another card is "
                "parents=[...], a wait on time or an external fact is kanban_schedule with wake_check "
                "(clock_only for a pure clock)." + _SKELETON_NOTE)
    if title and _DECISION_TITLE.match(title) and initial_status == "blocked":
        return f"{ERROR_CODE}: decision-style card needs human_request."
    return None


def sweep_agent_owned_blocks(conn: sqlite3.Connection, *, now: Optional[int] = None,
                             grace: int = AGENT_BLOCK_GRACE_SECONDS) -> list[str]:
    """One triage respawn, with evidence, for this gateway's agent-owned blocks older than *grace*."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db import _json_dict
    from hermes_cli.kanban_wake_check import foreign_ids

    # Pending writes can retain a worker's copy after its run ends. Execution
    # transitions belong to that worker; maintenance belongs to the creator.
    copies = foreign_ids(conn)
    now = int(now if now is not None else time.time())
    escalated: list[str] = []
    rows = conn.execute(
        "SELECT id, block_kind FROM tasks WHERE status = 'blocked' AND human_request IS NULL "
        "AND current_run_id IS NULL AND (block_kind IS NULL OR block_kind IN ('capability','transient','needs_input'))"
    ).fetchall()
    for row in rows:
        tid = row["id"]
        if tid in copies:
            continue
        ev = conn.execute(
            "SELECT id, payload, created_at FROM task_events WHERE task_id = ? "
            "AND kind IN ('blocked','block_loop_detected') ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        if ev is not None:
            started, ev_id = int(ev["created_at"]), int(ev["id"])
            payload = _json_dict(ev["payload"])
        else:
            t = conn.execute("SELECT created_at FROM tasks WHERE id = ?", (tid,)).fetchone()
            started, ev_id, payload = int(t["created_at"] or 0), 0, {}
        if now - started < grace or payload.get("blocker_id"):
            continue
        if conn.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'agent_block_escalated' "
                        "AND id > ? LIMIT 1", (tid, ev_id)).fetchone():
            continue
        evidence = (f"Agent-owned block ({row['block_kind'] or 'untyped'}) with no human ask, wake "
                    f"probe or worker for {(now - started) // 3600}h. Reason: "
                    f"{payload.get('reason') or '(none recorded)'}. Re-check whether the cause is "
                    "gone; then finish, schedule with a wake_check, route as assignment, or give a "
                    "real human_request.")
        with kb.write_txn(conn):
            changed = conn.execute(
                "UPDATE tasks SET status = 'triage', block_kind = 'transient' "
                "WHERE id = ? AND status = 'blocked' AND human_request IS NULL", (tid,)).rowcount
            if changed:
                kb._append_event(conn, tid, "agent_block_escalated", {
                    "reason": evidence, "kind": row["block_kind"], "blocked_at": started})
                escalated.append(tid)
    return escalated


RECHECK_EVENT = "ask_rechecked"
NAMED_CLOSED_CODE = "named_cards_closed"
# "macbook/t_..." is another board's card: this board cannot judge it, so it is not collected.
_TASK_ID = re.compile(r"(?<![\w/])t_[0-9a-f]{8,16}\b")


def _named_task_ids(title, request: dict, own_id: str) -> list[str]:
    # Premise only: a card cited as proof in ``evidence`` (a done probe) is not what the ask waits on.
    text = " ".join([str(title or "")] + [str(request.get(k) or "") for k in ("action", "why_human")])
    return sorted({m.group(0) for m in _TASK_ID.finditer(text)} - {own_id})


_SUCCESSOR_KEYS = ("verified_cards", "created_cards")
_SUCCESSOR_DEPTH = 3


def _successor_open(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when a closed card handed its work to a successor that is still open.

    Cards here often complete by filing their work as new cards (``verified_cards`` / ``created_cards`` in the
    newest ``completed`` event); such a card resolved nothing, so an ask naming it still stands. Successors are
    followed up to ``_SUCCESSOR_DEPTH`` hops; an id this board does not hold counts as open. [daveey]
    """
    from hermes_cli.kanban_db import _json_dict

    seen, frontier = {task_id}, [task_id]
    for _ in range(_SUCCESSOR_DEPTH):
        nxt: list[str] = []
        for tid in frontier:
            ev = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'completed' ORDER BY id DESC LIMIT 1",
                (tid,)).fetchone()
            payload = _json_dict(ev["payload"]) if ev is not None else {}
            for key in _SUCCESSOR_KEYS:
                ids = payload.get(key)
                for sid in ids if isinstance(ids, list) else ():
                    if isinstance(sid, str) and sid and sid not in seen:
                        seen.add(sid)
                        nxt.append(sid)
        if not nxt:
            return False
        marks = ",".join("?" for _ in nxt)
        rows = {r["id"]: r["status"] for r in conn.execute(
            f"SELECT id, status FROM tasks WHERE id IN ({marks})", nxt).fetchall()}
        if len(rows) != len(nxt) or any(s not in ("done", "archived") for s in rows.values()):
            return True
        frontier = nxt
    return False


def _action_line(request) -> str:
    return " ".join(str((request or {}).get("action") or "").split())


def standing_ask_error(conn: sqlite3.Connection, task_id: str, title, request) -> Optional[tuple[str, str, dict]]:
    """``(code, reason, extra)`` when a standing ask no longer holds, else None.

    (a) the relay / route-gap lint refuses it today (an ask filed before a rule landed is never re-linted);
    (b) it names at least one card and every named card is done or archived on this board (its premise is gone).
    An id this board does not hold counts as open: it cannot be verified here. A done card whose newest
    completion handed off to a still-open successor counts as open too (``_successor_open``).
    """
    if not isinstance(request, dict):
        return None
    err = agent_relay_ask_error(_action_line(request), request) or agent_route_gap_error(request)
    if err:
        return (RELAY_ASK_CODE if err.startswith(RELAY_ASK_CODE) else ROUTE_GAP_CODE), err, {}
    named = _named_task_ids(title, request, task_id)
    if not named:
        return None
    marks = ",".join("?" for _ in named)
    rows = {r["id"]: r["status"] for r in conn.execute(
        f"SELECT id, status FROM tasks WHERE id IN ({marks})", named).fetchall()}
    if len(rows) != len(named) or any(s not in ("done", "archived") for s in rows.values()):
        return None
    if any(_successor_open(conn, t) for t in named):
        return None
    closed = ", ".join(f"{t} ({rows[t]})" for t in named)
    return (NAMED_CLOSED_CODE,
            f"{NAMED_CLOSED_CODE}: every card this ask names is closed ({closed}), so its premise is likely gone.",
            {"named": named})


def sweep_standing_asks(conn: sqlite3.Connection) -> list[str]:
    """Re-check blocked cards that carry a human_request; move void ones to agent triage.

    ``sweep_agent_owned_blocks`` skips blocks with a human ask and the ask lint runs only when an agent files the
    block, so a standing ask was never looked at again: on 10-30 11 of 25 hub asks were void (filed before a lint
    rule, or every card they named long done). One ``ask_rechecked`` move per block event; a card with a live run
    is left alone; an ask re-filed unchanged after a recheck stands (the agent that re-checked it kept it). The
    card's ask is cleared so ``sweep_stale_triage`` hands it to an agent; the event keeps the old ask. [daveey]
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db import _json_dict
    from hermes_cli.kanban_wake_check import foreign_ids

    # Retaining a foreign copy for its host-local ask probe does not grant policy
    # authority: event-id dedup is local, so only the card's home may recheck it.
    copies = foreign_ids(conn)
    moved: list[str] = []
    rows = conn.execute(
        "SELECT id, title, human_request FROM tasks WHERE status = 'blocked' AND human_request IS NOT NULL "
        "AND current_run_id IS NULL").fetchall()
    for row in rows:
        tid = row["id"]
        if tid in copies:
            continue
        request = _json_dict(row["human_request"])
        if not request:
            continue
        ev = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind IN ('blocked','block_loop_detected','created') "
            "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        ev_id = int(ev["id"]) if ev is not None else 0
        prior = [(int(r["id"]), _json_dict(r["payload"])) for r in conn.execute(
            "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = ?", (tid, RECHECK_EVENT)).fetchall()]
        if any(pid > ev_id for pid, _ in prior):
            continue
        found = standing_ask_error(conn, tid, row["title"], request)
        if found is None:
            continue
        code, reason, extra = found
        if any(p.get("code") == code and _action_line(p.get("human_request")) == _action_line(request)
               for _, p in prior):
            continue
        with kb.write_txn(conn):
            changed = conn.execute(
                "UPDATE tasks SET status = 'triage', block_kind = 'transient', human_request = NULL "
                "WHERE id = ? AND status = 'blocked' AND human_request IS NOT NULL AND current_run_id IS NULL",
                (tid,)).rowcount
            if changed:
                kb._append_event(conn, tid, RECHECK_EVENT, {
                    "code": code, "human_request": request, **extra,
                    "reason": reason + " Re-check the ask: finish the card, route the work to an agent, or "
                    "re-file a real human_request."})
                moved.append(tid)
    return moved


def sweep_blocked_cards(conn: sqlite3.Connection, result) -> None:
    """Dispatcher-tick entry for the parked-card sweeps; each is isolated so one failing never skips the others.
    The third gives each continuation_exhausted park its resume requester (``kanban_resume_requester``)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_resume_requester import ensure_recovery_cards
    for name, sweep, out in (("agent-owned block", sweep_agent_owned_blocks, result.agent_block_escalated),
                             ("standing-ask recheck", sweep_standing_asks, result.ask_rechecked),
                             ("resume requester", ensure_recovery_cards, [])):
        try:
            out.extend(sweep(conn))
        except sqlite3.Error:
            kb._log.exception("kanban: %s sweep failed", name)
