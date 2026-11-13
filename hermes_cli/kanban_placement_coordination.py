"""Conservative coordination-tool portability, backed by operator Board attestations.

Labels are rollout attestations, not credentials or permission grants. Only publish them
once the installed tool has passed an actual worker proof. Unknown actions stay home.
The pickup gate checks the actual destination again, including after a pool changes.
"""
from __future__ import annotations

import json
import math
import re
import time

REASON = "needs worker coordination capability"
ACTION_REASON = "needs home coordination action"
# Intentionally narrower than the CLI: status and pull are the reader install's proof.
# A possessive (``hermes-hub's older core``) names the host in prose; it is not an invocation.
_HUB = re.compile(r"(?<![\w/.-])hermes-hub\b(?![/.-]|['\u2019]s(?![\w-]))", re.IGNORECASE)
_CROSS = re.compile(r"(?<![\w/.-])cross_task\b(?![/.-]|['\u2019]s(?![\w-]))", re.IGNORECASE)
_HUB_ACTIONS = set(("status pull sync sessions inbox tasks where links request requests "
                    "inspect-legacy reconcile-legacy inspect-alias retire-alias adopt-task "
                    "create-task depend outcomes install-watch watch diff-shared").split())
_EXECUTION = re.compile(r"(?:\b(?:run|execute|invoke|call|use|via|with)\s+`?|`|[;$|&]\s*)$", re.IGNORECASE)
# ``hub-admin '<command>'`` runs the command ON the hub with its write access: a hermes-hub action inside the
# still-open quote acts with the hub's identity and replica, so it needs ``capability:hub-write`` (the hub-admin
# attestation), not this worker's own hermes-hub install. A worker's ``hermes-hub outcomes --id`` reads only its
# OWN sent requests (the hub's request read back as ``state: null`` on worker-1, 11-13), so bare spellings stay home.
# The lookbehind also rejects a path-qualified spelling (``~/.hermes/bin/hub-admin '...'``): the hub-write
# attestation covers the bare ``hub-admin`` on PATH, not an arbitrary install path, so such a card keeps the
# wrapped action's own verdict (ACTION_REASON: stays home). Deliberate: the fail-closed direction.
_HUB_ADMIN = re.compile(r"(?<![\w/.-])hub-admin\s+(['\"])(?:(?!\1).)*$", re.IGNORECASE)
HUB_WRITE = "capability:hub-write"


def _actions(text, tool, known):
    """Recognize action tokens or explicit invocations, not host/repository names.

    Unknown commands fail closed in command syntax (quotes, execution verbs,
    shell separators, or a standalone command line). A prose mention alone is
    not execution intent. Do not exempt the containing sentence/line: other
    home markers remain independently binding.
    """
    for match in tool.finditer(text):
        tail = text[match.end():]
        action = re.match(r"[ \t]+([\w-]+)(?![\w-])", tail)
        # Also accept the actual tool-call spelling, including unknown actions.
        call = re.match(r"\(\s*action\s*=\s*['\"]([^'\"]+)['\"]", tail)
        value = (call.group(1) if call else action.group(1) if action else "").lower()
        prefix = text[:match.start()]
        line = prefix.rsplit("\n", 1)[-1]
        standalone = not line.strip() and not tail.strip(" \t`\n")
        command_line = (not line.strip() and action is not None
                        and not tail[action.end():].strip(" \t`\n"))
        explicit = _EXECUTION.search(prefix)
        # A quoted executable name (`hermes-hub` CLI shim) is documentation,
        # unlike a quoted command with an action (`hermes-hub unknown-command`).
        if explicit and explicit.group().strip() == "`" and not action:
            explicit = None
        if call or value in known or explicit or standalone or command_line:
            end = match.end() + (call.end() if call else action.end() if action else 0)
            yield match.start(), end, value


_TOOLS = {
    "token-broker": "capability:token-broker",
    "hermes-core-wt": "capability:core-ship",
    "tailnet_exec": "capability:tailnet-exec",
}


# Explicit task contracts, not authority inferred from prose. These declarations
# describe existing restricted access; they never install credentials or grant writes.
ACCESS_LABELS = {"ssh:mbp": "capability:ssh-mbp", "ssh:mini": "capability:ssh-mini",
                 "hub:read": "capability:hub-read", "hub:write": "capability:hub-write"}
# Declared access must mirror the Board's worker/src/worker_placement.ts supportedRequirement
# and requirementLabel contract. Deploy Board support before extending this set: otherwise
# enrolled cards queue forever with task_requirement_unsupported despite attested access.
# SSH requirements remain unsupported; read and write each require their own attestation.
BOARD_PLACEABLE_ACCESS: frozenset = frozenset({"hub:read", "hub:write"})
BOARD_REASON = "declared access the Board cannot place"


def board_refusal(requires):
    """:data:`BOARD_REASON` when ``requires`` declares access the Board's pool selection cannot carry."""
    return BOARD_REASON if any(r in ACCESS_LABELS and r not in BOARD_PLACEABLE_ACCESS
                               for r in requires or []) else None


_ACCESS = (
    (re.compile(r"\bssh\b", re.IGNORECASE), {"ssh:mbp", "ssh:mini"}),
    (re.compile(r"\bon the hub\b|\bthe hub's\b|\bjournalctl\b|\bkanban\.db\b", re.IGNORECASE),
     {"hub:read"}),
)


# hub-read's subcommands (hub ``~/.hermes/bin/hub-read``): ``ssh hermes-hub <sub>`` runs that restricted reader,
# not a shell. A bare ``ssh``, ``ssh hermes-hub <anything else>`` and ``ssh <other host>`` keep pinning.
HUB_READ_SUBCOMMANDS = ("cat", "tail", "head", "ls", "grep", "sqlite", "status", "journal", "units", "git", "help")
_HUB_READ_SSH = re.compile(r"\bssh[ \t]+hermes-hub[ \t]+(?:" + "|".join(HUB_READ_SUBCOMMANDS) + r")(?![\w-])",
                           re.IGNORECASE)
# A Hermes state path (same roots as ``kanban_placement.HERMES_STATE_PATH``). The path is an allow-list of path
# characters, so a shell operator glued to it (``&&``, ``;``, ``:``, ``|``, ``$(``) ends it and stays in the text.
# The match must also end its shell word (:func:`_ends_word`), or a quoted, brace, ``$``, ``\\`` or glob remainder
# (``logs/'..'``, ``logs/{..,x}``, ``logs/$(echo ..)``) would hide a ``..`` from :func:`_under_roots`
# (reviews 5877418508, 5877782890). ``%`` is literal in shell (``%2e%2e`` is a directory name, not ``..``).
_STATE_PATH = re.compile(r"(?:~|/home/[A-Za-z0-9_][\w.-]*)/\.hermes/([\w.@+%~/-]*)")
_QUOTES = "'\"`"
# After the path and its closing quote/paren, if any: at most one sentence mark, then the end or whitespace; or a
# shell operator, which ends the word.
_WORD_END = re.compile(r"[.,:;!?]?(?:$|\s)|[&;|<>]")


def _ends_word(text, start, end) -> bool:
    """Whether the state path ``text[start:end]`` is its whole shell word, so no remainder can hide a ``..``.

    A trailing quote counts only when the same quote opened the word right before the prefix (that quote at the
    start of the text or after whitespace); any other quote may open a new segment (``logs/' '/../../.env``). One
    ``)`` may close prose or ``$(...)``. Then at most one sentence mark, so ``logs/''..`` and ``$(echo logs/)..``
    (both resolve to ``~/.hermes``) keep pinning."""
    q = text[start - 1] if start and text[start - 1] in _QUOTES else ""
    if q and start >= 2 and not text[start - 2].isspace():
        # A quote glued to something else (``--file="``, ``x''``) cannot be shown to be a closing one, and as an
        # opening one its word runs on past whitespace (review 5893721470 nit): keep the path.
        return False
    i = end
    if q:
        # An opening quote must close right here: whitespace inside the quoted word (``"~/.hermes/logs/a b/../.env"``)
        # is part of the path, so a later close may hide a ``..`` (review 5892779312).
        if not text.startswith(q, i):
            return False
        i += 1
    if text.startswith(")", i):
        i += 1
    return bool(_WORD_END.match(text, i))


def hub_read_roots(cfg) -> tuple:
    """``kanban.placement.hub_read_roots`` as segment tuples (paths relative to ``~/.hermes``); () when unset.

    Fails closed: a malformed list, or an absolute, empty or ``.``/``..`` entry, clears nothing."""
    roots = cfg.get("hub_read_roots") if isinstance(cfg, dict) else None
    if not isinstance(roots, list):
        return ()
    out = []
    for root in roots:
        segs = tuple(root.strip().rstrip("/").split("/")) if isinstance(root, str) else ()
        if segs and not root.strip().startswith("/") and all(s not in ("", ".", "..") for s in segs):
            out.append(segs)
    return tuple(out)


def _under_roots(path: str, roots) -> bool:
    segs = path.rstrip("/").split("/")
    # Dot-only segments (``.``, ``..``) are refused before a sentence's trailing ``.`` is trimmed, so ``logs/..``
    # never reads as ``logs``.
    if any(s.strip(".") == "" for s in segs):
        return False
    segs[-1] = segs[-1].rstrip(".")
    return any(tuple(segs[:len(r)]) == r for r in roots)


def without_read_paths(text, cfg, *, target=None, requires=None):
    """``text`` minus ``~/.hermes/<p>`` paths under :func:`hub_read_roots`, for a declared and attested ``hub:read``.

    Only the path token goes, so a marker that merely names the file being read (``hermes-sync`` in
    ``~/.hermes/logs/hermes-sync.log``, ``cron`` in ``~/.hermes/cron/...``) stops pinning, while every marker outside
    it (a write verb such as ``restart the gateway`` or ``systemctl``) still pins. [daveey] t_01a1cf7175102c70"""
    roots = hub_read_roots(cfg)
    if not roots or "hub:read" not in set(requires or []):
        return text
    if not attested(destinations(cfg, target), access_requirements(requires)):
        return text
    return _strip_read_paths(text, roots)


# A standalone ``..`` path segment anywhere (``cd ~/.hermes/logs && cat ../.env``) can walk out of a root that
# per-token stripping already cleared, so such text keeps every path (review 5892779312).
_DOTDOT = re.compile(r"(?<![\w.~-])\.\.(?![\w.~-])")
# A ``..`` need not be literal (review 5893721470 nit): quotes and backslashes join dots (``.\.``, ``.''.``), and
# command or parameter expansion computes one (``$(dirname "$PWD")``, ``$(printf "\56\56")``, ``${D%logs/}``). So
# text with any of these keeps every path, as does a directory change other than into a plain absolute path outside
# ``~/.hermes`` (``cd ~/.hermes/logs`` then ``cd -``, ``popd`` or a relative path walks out of a root).
_SPLIT = re.compile(r"[\\'\"`]")
_EXPANSION = re.compile(r"\$[({'\"]")
_CHDIR = re.compile(r"(?<![\w.-])(?:cd|pushd|popd)(?![\w.-])(?![ \t]+(?:~|/)(?![^\s;&|]*\.hermes)[\w./-]*(?=$|[\s;&|)]))")


def _strip_read_paths(text, roots):
    if _DOTDOT.search(text) or _DOTDOT.search(_SPLIT.sub("", text)) or _EXPANSION.search(text) or _CHDIR.search(text):
        return text
    # Keep a separator so surrounding backtick runs cannot merge and hide later markers.
    return _STATE_PATH.sub(
        lambda m: " " if _ends_word(text, m.start(), m.end()) and _under_roots(m.group(1), roots) else m.group(0),
        text)


def requirements(text):
    """Return required labels and an unsupported-action refusal, without changing text."""
    labels, refusal, _ = _requirements(text)
    return labels, refusal


def _requirements(text):
    """:func:`requirements` plus the text span that matched a refusal (None without one)."""
    labels = set()
    # Reader/broker attestations do not grant managed-worker board administration.
    if admin := re.search(r"\binstall-boards\.py\b|\bhermes kanban boards (?:create|subscribe)\b|"
                          r"\bcreate board replicas\b|\battach primary/pickup subscriptions\b",
                          text, re.IGNORECASE):
        return labels, ACTION_REASON, admin.group()
    for start, end, action in _actions(text, _HUB, _HUB_ACTIONS):
        if _HUB_ADMIN.search(text[:start].rsplit("\n", 1)[-1]):
            labels.add(HUB_WRITE)
            continue
        if action not in ("status", "pull"):
            return labels, ACTION_REASON, text[start:end]
        labels.add("capability:coordination-read")
    for start, end, action in _actions(text, _CROSS, {"read", "list", "comment"}):
        if action not in ("read", "list"):
            return labels, ACTION_REASON, text[start:end]
        labels.add("capability:coordination-read")
    if re.search(r"\b(?:parent|sibling)[ -]card (?:lookup|read)|\bkanban_show\b", text, re.IGNORECASE):
        labels.add("capability:coordination-read")
    for tool, label in _TOOLS.items():
        if re.search(r"\b" + re.escape(tool) + r"\b", text, re.IGNORECASE):
            labels.add(label)
    return labels, None, None


def requirement_label(name):
    """The attested label a declared ``requires`` entry needs: access names map through :data:`ACCESS_LABELS`
    (``hub:write`` -> ``capability:hub-write``; no machine carries ``capability:hub:write``), others to
    ``capability:<name>``."""
    return ACCESS_LABELS.get(name, f"capability:{name}")


def access_requirements(requires):
    """Labels for declared access; unknown names remain gateway-scoped."""
    return {ACCESS_LABELS[r] for r in requires or [] if r in ACCESS_LABELS}


def destinations(cfg, target=None):
    """No pool inference for an explicit legacy heavy destination."""
    if target:
        return [target]
    from hermes_cli import kanban_capability_registry as reg
    pools = cfg.get("worker_pools")
    gateway = reg.self_gateway()
    names = pools.get(gateway) if isinstance(pools, dict) else None
    if (not isinstance(names, list) or not names or len(names) > 64
            or not all(isinstance(n, str) and n.strip() for n in names)):
        # The worker queue: every machine carrying this gateway's pool label is a possible destination.
        from hermes_cli import kanban_placement as placement
        label = placement._Cfg(cfg).queue_label(gateway)
        return placement.label_machines(label) if label else []
    return names


FRESH_S = 300
# ``last_seen`` is stamped by the Board's clock, and a just-refreshed entry's margin is sub-second (worker-7: 0.06 s),
# so a host whose clock lags the Board by ~0.1 s reads it as from the future. A few seconds of future skew is lag,
# not a bad stamp; further ahead stays stale. Queue liveness (label_machines) and attestation (eligible) share it.
SKEW_S = 5


def _machines():
    from hermes_cli import kanban_worker_node as wn
    snapshot = wn.registry()
    machines = snapshot.get("machines") if isinstance(snapshot, dict) else None
    return machines if isinstance(machines, dict) else None


def _fresh(entry, now):
    try:
        age = now - float(entry.get("last_seen")) / 1000
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(age) and -SKEW_S <= age <= FRESH_S


def eligible(names, machines, *, now=None):
    """Members that can lease work now: active, unheld, advertising pickup, fresh. None = malformed entry.

    The Board's selection skips the rest too (``worker_placement.ts::available``), so they can never take the card.
    A missing entry is a machine the Board does not know, so it leases nothing either. [daveey]"""
    from hermes_cli import kanban_worker_node as wn
    now = time.time() if now is None else now
    out = []
    for name in names:
        entry = machines.get(name)
        if entry is None:
            continue
        if not isinstance(entry, dict):
            return None
        if (entry.get("state") == "active" and not entry.get("hold") and wn.advertises_pickup(entry)
                and _fresh(entry, now)):
            out.append(name)
    return out


def _carries(entry, required):
    labels = entry.get("labels")
    return (isinstance(labels, list) and all(isinstance(v, str) for v in labels)
            and required.issubset(labels))


def attested(names, required):
    """Every destination that could take the card carries a recent operator label; cached records age out.

    Only eligible members are judged (:func:`eligible`): a held, inactive, pickup-off or stale member is skipped,
    not a refusal. kanban-1663abf6ab: worker-1 on hold made every access-label card on the hub home-only while
    workers 2-6 carried the labels. Fail closed on a malformed registry, no eligible member, or an eligible member
    without the labels (the claim gate rechecks the actual destination either way)."""
    machines = _machines() if names else None
    if machines is None:
        return False
    names = eligible(names, machines)
    return bool(names) and all(_carries(machines[name], required) for name in names)


def check(text, cfg, *, target=None, skills=None, requires=None):
    """Revalidate coordination tools and explicitly declared remote access."""
    required, refusal = requirements(text)
    required.update(skill_requirements(skills))
    required.update(access_requirements(requires))
    if refusal or not required:
        return refusal
    return None if attested(destinations(cfg, target), required) else REASON


def marker_text(text):
    """Only called after check succeeds. Extra operator markers use ORIGINAL text."""
    for start, end, action in reversed(list(_actions(text, _HUB, _HUB_ACTIONS))):
        if action in ("status", "pull"):
            text = text[:start] + text[end:]
    return re.sub(r"\btoken-broker\b", "", text, flags=re.IGNORECASE)


def access_text(text, cfg, *, target=None, requires=None):
    """``text`` minus the remote-access mentions (ssh, hub reads) whose labels EVERY destination attests.

    An unattested mention stays in the text, so its home marker still pins the card exactly as before.
    Kept apart from :func:`marker_text` so a card that never needed the attestation keeps its markers."""
    declared = set(requires or [])
    labels = access_requirements(declared)
    if not labels or not attested(destinations(cfg, target), labels):
        return text
    if "hub:read" in declared:
        # The restricted reader's own invocation, and reads under its roots (``kanban.placement.hub_read_roots``).
        text = _strip_read_paths(_HUB_READ_SSH.sub("", text), hub_read_roots(cfg))
    for rx, needed in _ACCESS:
        if declared & needed:
            text = rx.sub("", text)
    return text


def _claim_text(card):
    """The text the pickup gate judges: title plus body, minus evidence and the origin's placement note."""
    from hermes_cli.kanban_placement import without_evidence, without_placement_note
    return f"{card.get('title') or ''}\n{without_evidence(without_placement_note(card.get('body') or ''))}"


def _claim_requires(card):
    """The card's declared ``requires`` as a list of names, or None when it is malformed."""
    requires = card.get("requires")
    if requires is None or requires == "":
        return []  # Legacy rows have no declaration.
    if isinstance(requires, str):
        try:
            requires = json.loads(requires)
        except ValueError:
            return None
    if not isinstance(requires, list) or not all(isinstance(r, str) for r in requires):
        return None
    return requires


INVALID_REQUIRES = "invalid worker access requirements"


def claim_refusal(card, me):
    """Revalidate actual destination of enrolled cards, never override explicit pins."""
    if card.get("placement_policy") != "worker-first-v1":
        return None
    text = _claim_text(card)
    from hermes_cli.kanban_runs_on_capabilities import _loads
    # Wire rows can bypass creator validation. Never turn corrupt contracts into
    # empty requirements, or let one malformed card crash the dispatch loop.
    requires = _claim_requires(card)
    if requires is None:
        return INVALID_REQUIRES
    if refusal := check(text, {}, target=me, skills=_loads(card.get("skills"), []), requires=requires):
        return refusal
    from hermes_cli import kanban_mandatory_pin as mandatory
    if (pin := mandatory.requirements(card)) and not attested([me], pin):
        return mandatory.REASON  # this machine's core is not attested to refuse a pin it cannot serve
    return None


def pickup_refusals(card, names):
    """``{machine: {"reason", "quote"[, "missing_labels"]}}`` for each of ``names`` whose pickup gate
    (:func:`claim_refusal`) refuses ``card`` for a reason no change of that machine's state lifts. [daveey]

    t_01a1d560c6d3bc3c: worker-first placement reserved cards every worker's gate refused, every ~10 s for 42 min.
    Text the gate reads as a home-only action, or a malformed ``requires``, refuses on every machine (``quote`` is
    the matched text). Otherwise a member refuses when its registry entry lacks a label the card needs (text,
    skills, declared access, mandatory pin; ``quote`` names them). Liveness is NOT judged here: a held, stale or
    pickup-off member may take the card later, and a member the registry does not know, or an unreadable registry,
    is no refusal either. The card's ``placement_policy`` is ignored: the placement tick asks before reserving.
    """
    names = list(dict.fromkeys(n for n in names or [] if isinstance(n, str) and n))
    requires = _claim_requires(card)
    if requires is None:
        return {n: {"reason": INVALID_REQUIRES, "quote": str(card.get("requires"))[:200]} for n in names}
    required, refusal, quote = _requirements(_claim_text(card))
    if refusal:
        return {n: {"reason": refusal, "quote": quote} for n in names}
    from hermes_cli import kanban_mandatory_pin as mandatory
    from hermes_cli.kanban_runs_on_capabilities import _loads
    pin = mandatory.requirements(card)
    required |= skill_requirements(_loads(card.get("skills"), [])) | access_requirements(requires) | pin
    machines = _machines() if required and names else None
    if machines is None:
        return {}
    out = {}
    for name in names:
        entry = machines.get(name)
        labels = entry.get("labels") if isinstance(entry, dict) else None
        if not isinstance(labels, list):
            continue  # unknown to the registry: not this gate's refusal
        missing = sorted(required - {v for v in labels if isinstance(v, str)})
        if missing:
            out[name] = {"reason": mandatory.REASON if set(missing) <= pin else REASON,
                         "quote": ", ".join(sorted(required)), "missing_labels": missing}
    return out


def pool_refusal(card, names):
    """``{"reason", "quote", "machines"}`` when :func:`pickup_refusals` refuses ``card`` on EVERY one of ``names``
    (so no pool worker could ever claim it), else None. An empty pool is no refusal."""
    names = list(dict.fromkeys(n for n in names or [] if isinstance(n, str) and n))
    refused = pickup_refusals(card, names)
    if not names or len(refused) != len(names):
        return None
    first = refused[names[0]]
    return {"reason": first["reason"], "quote": first["quote"], "machines": refused}


def _free_slot(entry):
    metrics = entry.get("metrics") if isinstance(entry.get("metrics"), dict) else {}
    active, cap = metrics.get("active_card_runs"), entry.get("max_card_runs")
    return type(active) is int and type(cap) is int and 0 <= active < cap


def stranded(card, cfg, created_reason):
    """Pool workers that could run a home-waiting card now, or ``[]``. [daveey] kanban-1663abf6ab

    A card created while the pool did not attest its access labels (``created_reason`` == :data:`REASON`) stays
    home-only for good: enrollment is create-only. When an eligible pool member now carries every label the card
    needs and has a free slot, and the card is otherwise portable (its full ``portable_reason`` judged against just
    those members is None, so a home marker the early refusal hid still counts), the wait is the stale creation
    verdict, not capacity. Read-only and advisory."""
    from hermes_cli.kanban_mandatory_pin import REASON as PIN_REASON
    if created_reason not in (REASON, PIN_REASON) or card.get("placement_policy") or not isinstance(cfg, dict):
        return []
    from hermes_cli import kanban_capability_registry as reg
    from hermes_cli.kanban_placement import portable_reason
    from hermes_cli.kanban_runs_on_capabilities import _loads
    args = {k: card.get(k) for k in ("title", "body", "assignee", "workspace_kind", "workspace_path", "tenant",
                                     "project_id", "max_retries", "model_override", "provider_override",
                                     "model_pin_mandatory")}
    args.update(skills=_loads(card.get("skills"), []), requires=_loads(card.get("requires"), []),
                runs_on=_loads(card.get("runs_on"), None))
    if not isinstance(args["requires"], list):
        return []
    required = required_labels(args, cfg)  # same text as portability: carried inputs stripped
    pool = destinations(cfg)
    if not required or not pool:
        return []
    machines = _machines()
    names = eligible(pool, machines) if machines is not None else None
    if machines is None or not names:
        return []
    ready = [n for n in names if _carries(machines[n], required) and _free_slot(machines[n])]
    pools = {**(cfg.get("worker_pools") or {}), reg.self_gateway(): ready}
    return ready if ready and portable_reason(args, {**cfg, "worker_pools": pools}) is None else []


def required_labels(args, cfg=None):
    """Coordination labels a card's text, skills and declared access need; None for an unsupported action.

    Reads the same text as portability's :func:`check`: verified carried inputs are removed first, so a
    path such as ``.../evidence/token-broker-audit/BRIEF.md`` does not demand a label the check never did.
    """
    from hermes_cli import kanban_placement as placement
    from hermes_cli.kanban_carried_evidence import without_carried_inputs
    body = placement.without_evidence(placement.without_placement_note(args.get("body") or ""))
    text = without_carried_inputs(f"{args.get('title') or ''}\n{body}", placement.config() if cfg is None else cfg)
    required, refusal = requirements(text)
    if refusal:
        return None
    from hermes_cli.kanban_mandatory_pin import requirements as pin_requirements
    return (required | skill_requirements(args.get("skills")) | access_requirements(args.get("requires"))
            | pin_requirements(args))


def legacy_refusal(args, cfg=None):
    """Legacy automatic pins have no policy fence; do not apply coordination exemptions."""
    labels = required_labels(args, cfg)
    return labels is None or bool(labels)


def skill_requirements(skills):
    """Known workflow pins whose runtime tools are part of this rollout."""
    if isinstance(skills, str):
        skills = [skills]
    labels = set()
    for skill in skills or []:
        if skill == "token-broker":
            labels.add("capability:token-broker")
        elif skill in ("hermes-mods-repo-core-dev", "patching-local-hermes-workflow"):
            labels.add("capability:core-ship")
    return labels
