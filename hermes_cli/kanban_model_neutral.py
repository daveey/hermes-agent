"""Model-neutral Kanban specifications; execution receipts remain concrete.

Ingress rejects restrictions rather than silently accepting an ineffective pin.
Legacy routing reads use a separate projection and never rewrite author history.
Interactive sessions and the model catalog are deliberately outside this policy.
"""
from __future__ import annotations

import json
import re
from agent.model_routing import is_class_selector
from hermes_cli.models_catalog_static import _PROVIDER_MODELS

ERROR = 'Kanban is model-neutral: use a class/tier or capability requirement, not a concrete model/provider or mandatory pin'
# Match directives, not incidental named execution evidence or author exclusions.
_REQUIRED = re.compile(r'^\s*require_model\s*:\s*(?:true|yes|1)\s*$', re.IGNORECASE | re.MULTILINE)
def _model_names():
    """Offline catalog identity, not vendor-prefix guesses or live entitlement."""
    from hermes_cli.models_catalog_static import _PROVIDER_MODELS
    names = {name.lower().rsplit('/', 1)[-1] for rows in _PROVIDER_MODELS.values() for name in rows}
    # Catalog transport selectors (e.g. moa/default) are not model identities.
    # Otherwise ordinary board/profile references such as default/task become pins.
    names.difference_update({'default', 'auto'})
    # These are human shorthand for catalog families, not arbitrary claude-* tools.
    aliases = {alias for alias in ('opus', 'sonnet', 'fable', 'astra', 'luna', 'sol')
               if any(alias in re.split(r'[-.]', name) for name in names)}
    # Human family+version spelling must consume the entire identity. Derive
    # versions from the offline catalog, never accept arbitrary suffixes.
    versioned = set()
    for name in names:
        for alias in aliases:
            match = re.search(r'\b' + alias + r'-(\d+(?:[-.]\d+)*)(?:-|$)', name)
            if match:
                parts = re.split(r'[-.]', match[1])
                for joiner in ('-', '.'):
                    for separator in ('', '-', ' '):
                        versioned.add(alias + separator + joiner.join(parts))
    return '|'.join(re.escape(name) for name in sorted(names | aliases | versioned, key=lambda name: (-len(name), name)))


_MODEL = re.compile(r'(?:\b|(?<=independent)|(?<=primary))(?:' + _model_names() + r')(?![\w-]|\.[\w])', re.IGNORECASE)
# Bind the obligation to the route, rather than co-occurrence with unrelated
# words such as "required CI", "source only" or a tool-call bug report.
_QUALIFIER = r'(?:(?:the|a|an|only|model|provider|route|reviewer|review|independent|primary|exact[- ]head|on|to|with|use|using|run|by)\s+)*'
_ROUTE_NAME = r'(?:[\w-]+/)?' + _MODEL.pattern
_DIRECTIVE = re.compile(
    r'(?:\b(?:must|require|requires|required|mandatory|no fallback for)\s+'
    r'|(?:^|:)\s*(?:[-*]\s*)?(?:use|select|choose|pin|restrict)\s+)'
    + _QUALIFIER + _ROUTE_NAME
    + r'|' + _ROUTE_NAME + r'(?:/[\w-]+)?\s+(?:(?:review\s+|is\s+)?(?:required|mandatory|only)\b|must\s+be\s+used\b)',
    re.IGNORECASE,
)
# Provider identities are routing restrictions only in an obligation's route slot,
# not incidental provider/API implementation work.
_PROVIDER_NAME = r'\b(?:' + '|'.join(re.escape(p) for p in sorted(_PROVIDER_MODELS, key=lambda p: (-len(p), p))) + r')\b'
_PROVIDER_DIRECTIVE = re.compile(
    r'(?:\b(?:must|require|requires|required|mandatory|no fallback for)\s+'
    r'|(?:^|:)\s*(?:[-*]\s*)?(?:use|pin|restrict|select only|choose only)\s+)'
    + _QUALIFIER + _PROVIDER_NAME
    + r'(?:\s+(?:provider|route|reviewer))?(?=\s*(?:[.!?;,]|$|for (?:this|the) (?:review|task|card|run)\b))'
    + r'|(?:^|:)\s*' + _PROVIDER_NAME + r'\s+(?:(?:provider|route|reviewer)\s+)?'
    r'(?:(?:is\s+)?(?:required|mandatory|only)|must\s+be\s+used)'
    r'(?=\s*(?:[.!?;,]|$|for (?:this|the) (?:review|task|card|run)\b))',
    re.IGNORECASE,
)
_EVIDENCE = 'Historical run instruction (superseded and not active): '
_TEXT_GUIDANCE = ('; for superseded historical evidence only, put the unchanged line in a JSON string after '
                  + _EVIDENCE + '(do not quote active instructions)')
# Replace only the negated obligation phrase, never its surrounding clause: a
# trailing positive mandate must still be checked normally.
_NEGATED_OBLIGATION = re.compile(
    r'\b(?:no longer\s+(?:requires?|required|mandatory)|does? not\s+require|'
    r'non[- ]mandatory)\b', re.IGNORECASE)
_REMOVED_OBLIGATION = re.compile(
    r'^(\s*(?:[-*]\s*)?(?:remove|delete)\s+(?:the\s+)?(?:skill\s+)?'
    r'(?:text|instruction|wording)\s+that\s+)(?:requires?|mandates?)\b', re.IGNORECASE)


def _active_text(line):
    """Exclude a labelled JSON evidence string, never unquoted trailing policy."""
    stripped = line.lstrip()
    if stripped.startswith(_EVIDENCE):
        try:
            value, end = json.JSONDecoder().raw_decode(stripped[len(_EVIDENCE):])
        except ValueError:
            return line
        if isinstance(value, str):
            return stripped[len(_EVIDENCE) + end:]
    return line


def validate_selector(model=None, provider=None, *, mandatory=False):
    """Validate newly supplied selectors without consulting quota or credentials."""
    if any(value is not None and not isinstance(value, str) for value in (model, provider)):
        raise ValueError(ERROR)
    model = (model or '').strip() or None
    provider = (provider or '').strip() or None
    if mandatory or (model and not is_class_selector(model)) or (provider and provider != 'auto'):
        raise ValueError(ERROR)
    if provider and not model:
        raise ValueError(ERROR)
    return model, ('auto' if model else None)


def validate_explicit_selector(model=None, provider=None, *, mandatory=False):
    """Opt-in exact membership for local update and review request boundaries."""
    from agent.model_routing import exact_class_selector
    model, provider = validate_selector(model, provider, mandatory=mandatory)
    if model and not exact_class_selector(model):
        raise ValueError(ERROR)
    return model, provider


MANDATORY_FLAGS = ('pin_mandatory', 'model_pin_mandatory', 'require_model')


def validate_update_flags(payload):
    """Opt-in for local REST update/bulk only: null/false/integer zero are inert; anything else refuses.

    Raises ordinary ``ValueError(ERROR)``; no typed wire protocol. Not used by create/DB/selfwork admission.
    """
    metadata = getattr(payload, 'metadata', None) or {}
    values = [getattr(payload, key, None) for key in MANDATORY_FLAGS]
    if isinstance(metadata, dict):
        values += [metadata.get(key) for key in ('pin_mandatory', 'require_model')]
    if not all(value is None or value is False or (type(value) is int and value == 0) for value in values):
        raise ValueError(ERROR)


def standing_allowed(*values):
    """Direct standing promotion check: the UNCHANGED recognizer accepts every value."""
    try:
        validate_text(*values)
    except ValueError:
        return False
    return True


def standing_candidates(*texts):
    """S1/S2 only: deduped (raw_line, words) accepted at every stage before dedupe/limit.

    Raw body/comment bytes are untouched and remain visible elsewhere; the compactor and
    the default helpers keep their own extraction.
    """
    from agent import standing_instructions as si
    found = []
    for text in texts:
        found += si.restricted_explicit_lines(text, standing_allowed)
    return si.dedupe_candidates(found)


def standing_wait_words(texts, render, limit=3):
    """S2: first ``limit`` don't-wait words whose quoted form and actual message ``render(words)`` pass."""
    from agent import standing_instructions as si
    chosen = []
    for raw, words in standing_candidates(*texts):
        if len(chosen) == limit:
            break
        if si.says_dont_wait([words]) and standing_allowed(raw, words, f'"{words}"', render([*chosen, words])):
            chosen.append(words)
    return chosen


# Bounded role grammar: identity must occupy an execution-role slot, and that
# role must be requested (imperative/title) or the subject of an obligation.
# This intentionally does not classify arbitrary natural-language model mentions.
_ROLE_MODIFIERS = r'(?:(?:a|an|the|fresh|independent|primary|exact[- ]head|pinned)\s+)*'
_NAMED_ROLE = _ROLE_MODIFIERS + _ROUTE_NAME + r'\s+(?:review|reviewer|subagent|worker)\b'
_ROLE_REQUEST = re.compile(
    r'^\s*(?:\d+[.)]\s*|[-*]\s*)?(?:run|arrange|request|obtain|spawn|dispatch)\s+'
    + _ROLE_MODIFIERS, re.IGNORECASE)
_ROLE_TITLE = re.compile(r'^\s*(?:independent|primary|exact[- ]head)\s+', re.IGNORECASE)
_ROLE_NOUN = re.compile(_NAMED_ROLE, re.IGNORECASE)
_ROLE_TAIL = re.compile(
    r'\s*(?:(?:and|\+)\s+(?:exact[- ]head\s+)?(?:CI|tests?|approval)\s*)*'
    r'(?:(?:is|are)\s+)?(?:mandatory|required)\b', re.IGNORECASE)


def _requested_role(clause):
    """Parse request heads separately from complete role/obligation subjects.

    Punctuation does not erase receipt predicates. Only imperative heads may
    start after a label; a named role alone must occupy the complete clause.
    """
    for segment in re.split(r'[:,]', clause):
        segment = segment.strip()
        command = _ROLE_REQUEST.match(segment)
        if command:
            rest = segment[command.end():]
            if _ROLE_NOUN.match(rest):
                return True
            role = re.match(r'(?:review|subagent|worker)\b', rest, re.IGNORECASE)
            if role:
                complement = rest[role.end():]
                # Direct executor complement, not a preposition nested in the
                # reviewed subject ("review of code written by <model>").
                if re.fullmatch(r'\s+(?:with|using|by)\s+' + _ROLE_MODIFIERS + _ROUTE_NAME
                                + r'\s*[.!]?\s*', complement, re.IGNORECASE):
                    return True
                if re.search(r'\bwith\s+(?:a\s+)?pinned\s+' + _ROUTE_NAME
                             + r'\s+(?:subagent|reviewer|worker)\b', complement, re.IGNORECASE):
                    return True
        # A coordinated list is an obligation only when its predicate belongs
        # to this whole subject, not an arbitrary suffix in receipt prose.
        role = _ROLE_NOUN.match(segment)
        if role and _ROLE_TAIL.fullmatch(segment[role.end():].rstrip('.! ')):
            return True
    whole = clause.strip()
    role = _ROLE_NOUN.match(whole)
    if role and _ROLE_TITLE.match(whole):
        return bool(re.fullmatch(
            r'\s*(?:(?:through|via)\s+(?:[\w/-]+|[\w/-]+\s+card))?[.!]?\s*',
            whole[role.end():], re.IGNORECASE))
    return False


def validate_text(*values):
    """Reject positive machine directives and explicit named-model obligations.

    A free-form historical observation is not a restriction. Authorship markers,
    exclusions and negated restrictions are deliberately not rewritten here.
    """
    for text in values:
        for source_line in (text or '').splitlines():
            line = _active_text(source_line)
            if _REQUIRED.search(line):
                raise ValueError(f'{ERROR}; refused line: {source_line}{_TEXT_GUIDANCE}')
            # Comments carry authorship, not authority over text after their end.
            line = re.sub(r'<!-- implementer-models:.*?-->', '', line)
            # Inspect rendered inline identities, retaining the original stored bytes.
            line = re.sub(r'(`+|\*\*|__|\*|_)(.+?)\1', r'\2', line)
            for clause in re.split(r'(?<=[.!?;])\s+', line):
                clause = _NEGATED_OBLIGATION.sub('optional', clause)
                clause = _REMOVED_OBLIGATION.sub(r'\1mentions', clause)
                # Remove only the observed failed-route phrase, not a trailing
                # mandate ("...unavailable; use only <model>" still refuses).
                clause = re.sub(r'\bPinned route\s+' + _ROUTE_NAME + r'\s+is unavailable\b',
                                'Observed unavailable route', clause, flags=re.IGNORECASE)
                if not (_MODEL.search(clause) or _PROVIDER_DIRECTIVE.search(clause)):
                    continue
                # Mask only negative/exclusion wording up to a new positive
                # obligation, never the rest of a clause containing that mandate.
                clause = re.sub(
                    r'\b(?:(?:never|do not|must not)\s+(?:pin|require|restrict)'
                    r'|(?:implementer models|forbidden overlaps|excluded models|exclude)\s*:'
                    r'|differ from|not be reviewed by|other than|distinct from|not (?:the )?same as)'
                    r'.*?(?=\b(?:must(?!\s+not)|requires?|required|mandatory|'
                    r'no fallback for|(?:use|select|choose|pin|restrict)\s+(?:only\s+)?)\b'
                    r'|[,;]\s*(?:run|arrange|request|obtain|spawn|dispatch)\b|$)',
                    ' ', clause, flags=re.IGNORECASE)
                if re.fullmatch(
                    r'\s*(?:the )?(?:old|legacy|previous|historical) '
                    r'(?:configuration|policy|instruction|card|review|run|session) '
                    r'(?:required|mandated|pinned)\s+' + _MODEL.pattern + r'(?:\s+only)?[.!?;]?\s*',
                    clause, re.IGNORECASE,
                ):
                    continue
                # A bare requested-review title is operative; a sentence that
                # merely attributes a completed review is not. Do not scan for
                # "independent Opus" anywhere inside arbitrary provenance prose.
                named_review = re.fullmatch(
                    r'\s*(?:(?:primary|independent|exact[- ]?head)[ -]*)+'
                    + _MODEL.pattern + r'\s+(?:exacthead\s+)?review(?:\s+required)?'
                    r'(?:\s*[,;+].*)?[.!]?\s*', clause, re.IGNORECASE)
                required_review = re.search(
                    r'\b(?:needs?|requires?|must get)\s+(?:a\s+)?(?:fresh\s+)?'
                    r'(?:independent\s+)?review\s*\(\s*' + _ROUTE_NAME,
                    clause, re.IGNORECASE)
                if (_DIRECTIVE.search(clause) or _PROVIDER_DIRECTIVE.search(clause)
                        or named_review or required_review or _requested_role(clause)):
                    raise ValueError(f'{ERROR}; refused line: {source_line}{_TEXT_GUIDANCE}')


NEUTRAL_ROUTE = 'any qualified route'
_ROUTE_OCCURRENCE = re.compile(_ROUTE_NAME + r'(?:/[\w-]+)?|' + _PROVIDER_NAME, re.IGNORECASE)


def _passes(text):
    try:
        validate_text(text)
    except ValueError:
        return False
    return True


def _route_spans(line):
    """Map the validator's one-pass Markdown rendering back to original bytes."""
    positions, last = [], 0
    for match in re.finditer(r'(`+|\*\*|__|\*|_)(.+?)\1', line):
        positions.extend(range(last, match.start()))
        positions.extend(range(*match.span(2)))
        last = match.end()
    positions.extend(range(last, len(line)))
    rendered = ''.join(line[index] for index in positions)
    return [(positions[match.start()], positions[match.end() - 1] + 1)
            for match in _ROUTE_OCCURRENCE.finditer(rendered)]


def neutral_line(line):
    """The refused line with only the route identities its obligations name made neutral.

    Every other byte, including non-model gates sharing the line and any mention the
    recognizer accepts (exclusions, provenance), stays verbatim and active. Returns
    None when no identity substitution can make the line acceptable (a nameless
    machine directive such as ``require_model: true``). No lexical guess decides
    which prose is a gate: only the unchanged ingress recognizer is consulted.
    """
    spans = _route_spans(line)

    def render(chosen):
        out, last = [], 0
        for index, (start, end) in enumerate(spans):
            if index in chosen:
                out += [line[last:start], NEUTRAL_ROUTE]
                last = end
        return ''.join(out) + line[last:]

    chosen = set(range(len(spans)))
    if not spans or not _passes(render(chosen)):
        return None
    # Restore each identity the recognizer does not need neutralized, so author
    # exclusions and execution provenance on the same line are never rewritten.
    for index in range(len(spans)):
        if _passes(render(chosen - {index})):
            chosen.discard(index)
    return render(chosen)


def retained_line(line, separator='\n'):
    """Quote a refused line as history and keep its neutralized remainder active.

    Returns None when the line has no acceptable neutral form; callers then decide
    conservatively rather than silently inactivating unrecognized obligations.
    """
    neutral = neutral_line(line)
    if neutral is None:
        return None
    return _EVIDENCE + json.dumps(line, ensure_ascii=False) + separator + neutral


def forwarded_text(text):
    """Retain superseded legacy directives as labelled evidence, not new policy.

    A refused line's other obligations are forwarded active with the named route
    neutralized; only a nameless pure directive line is quoted alone.
    """
    lines = []
    for line in (text or '').splitlines():
        if not _passes(line):
            if _REQUIRED.fullmatch(line):
                line = _EVIDENCE + json.dumps(line, ensure_ascii=False)
            else:
                retained = retained_line(line)
                if retained is None:
                    validate_text(line)  # Refuse rather than discard unknown mixed obligations.
                line = retained
        lines.append(line)
    return '\n'.join(lines)


def forwarded_title(title, fallback):
    """Keep ordinary recovery titles; legacy mandates belong in body evidence."""
    try:
        validate_text(title)
    except ValueError:
        return fallback
    return title


def effective_selector(task):
    """Ignore legacy concrete restrictions without mutating stored author evidence."""
    model = str(getattr(task, 'model_override', None) or '').strip()
    return (model if is_class_selector(model) else 'class:standard'), 'auto'


def routing_view(task):
    """A next-admission view, never a replacement for the historical author row."""
    from copy import copy
    view = copy(task)
    view._routing_source = getattr(task, '_routing_source', task)
    model = str(getattr(task, 'model_override', None) or '').strip()
    view.model_override = model if is_class_selector(model) else None
    view.provider_override = 'auto' if view.model_override else None
    view.model_pin_mandatory = None
    return view


def validate_review(metadata):
    """New handoff metadata is policy, unlike past run/model evidence."""
    model, provider = validate_selector(metadata.get('review_model'), metadata.get('review_provider'),
                                        mandatory=metadata.get('pin_mandatory') or metadata.get('require_model'))
    return (model, provider, 'review_override') if model else None
