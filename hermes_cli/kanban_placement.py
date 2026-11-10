"""Default Kanban placement: a card with no local-only need runs on another gateway. [daveey]

A gateway whose own machine must stay quiet (a laptop the user works on) maps itself to a target
gateway in ``kanban.placement.route`` (shared config, e.g. ``{"macbook": "hub"}``). ``kanban_create``
then asks :func:`decide` before writing a local card. Unless something pins the card here, it is
created on the target through ``hermes-hub create-task <gw> ... --origin-session <chat>``: the same
origin-bound path cross-gateway work already uses, so the card still shows in the chat that asked
for it, and it runs (and burns CPU) on the target.

A card stays local when any of these holds:

* the caller pinned it: ``placement="local"``;
* it has parents and ``defer_parents`` is off. With it on (the default) a card with parents and no
  other local-only need is created here as a local dependency and returns ``{"where": "local",
  "defer": <target>}``: ``kanban_create`` records a ``placement_deferred`` event, and when its parents
  finish the dispatcher runs :mod:`hermes_cli.kanban_placement_forward` instead of a worker, which
  creates the card on the target (parent handoffs in the body, same origin chat) and parks the local
  card until the target's card is done, so local grandchildren still wait for the real work;
* it cannot be expressed remotely: a project, a ``dir`` or
  ``worktree`` workspace, a non-default board, an assignee the target has no profile of (its
  hermes-hub ``gateway`` record lists them; ``assignees`` names extra always-placed ones), courier
  (origin) creation, or creation
  options ``hermes-hub create-task`` cannot carry (an initial status, a human request, a tenant). A
  hermes-hub client with ``create-task --option`` (1.8.0+) carries skills, the runtime budget, priority,
  model/provider pins, goal mode, triage, the completion contract and the rest of
  :data:`CARRIED_OPTIONS`; with an older client a model/provider pin, goal mode, triage or a non-local
  completion contract keeps the card local, and skills and the runtime budget ride in the body text;
* it declares a ``requires`` capability this gateway holds and the target does not;
* it ``requires`` a profile tool the target cannot satisfy: anything outside
  ``carried_requires`` (default terminal, file, git, kanban; so host_repo and unknown names), a
  tool the target's profile lacks when the target publishes ``profile-tools:<profile>:<tool>``
  in its capability record, or any profile tool with a hermes-hub client that cannot carry
  ``requires`` as a field;
* its title, body or skills match a local marker (``kanban.placement.local_markers`` replaces the
  built-in list, ``extra_local_markers`` extends it): Chrome, Keychain, TCC, Apple apps, launchd,
  the Desktop app, a Desktop DOM/CDP readback, this Mac's Keychain... A bare host name (MacBook,
  the mini) pins only when it is the subject of the title. A body mention — a repro, or "verify
  live on the hub and the MacBook" — is not a local-only need. A product name (Baby Bear) in the
  title pins only when the card names no Linux, or also names Apple/device work;
* the target is not live: this gateway's hermes-hub sync has not succeeded recently, or the
  target's capability record is stale. A name hermes-hub has no record of at all is refused as
  unknown, with the gateway names it does know.

Heavy work (tests, builds, benchmarks, installs: :data:`DEFAULT_HEAVY_MARKERS`, replaced by
``kanban.placement.heavy_markers``) with no local-only need goes to a worker node instead of the target
when ``kanban.placement.heavy_route`` maps this gateway to one (``{"macbook": "worker-1"}``): the card is
created here with ``runs_on=[machine:<worker>]`` and the worker claims it through the Board lease, so it
keeps its parents and its chat. A card that names the target's own host (``hermes-hub``, "on the hub")
keeps the normal route, and so does any card when the worker is not an active pickup machine.

A worker on the target that still hits a local-only wall blocks with ``needs=[...]``; capability
routing (:mod:`hermes_cli.kanban_capability_routing`) then opens an owner card back here.

After queuing the card, ``kanban_create`` waits up to ``wait_seconds`` (default 10) for the target's
answer (:func:`await_task_id`) and returns its task id like a local create. Still pending, it returns
``status: "queued"`` with a note that the card IS created: a chat model that read ``task_id: null``
as "not created" once called kanban_create again and made a duplicate card. An identical retry
maps to the same request id (:func:`request_id`) and is never sent twice.

A pinned name resolves aliases first (:func:`canonical_pin`): ``hermes-<gw>`` (the hub's host is
``hermes-hub``, its gateway ``hub``) and ``kanban.placement.aliases``.

Unset ``route`` (upstream default) never places anything remotely.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

from hermes_cli import kanban_capability_registry as reg

HUB_TIMEOUT = 60
DEFAULT_HUB_LIVE_SECONDS = 600
DEFAULT_CREATOR_SESSION = "kanban-placement"

# "macOS" as a word, not a CI runner label or file/skill name (macos-latest, macos-release,
# hermes-state-macos.yml): a GitHub-hosted macOS job runs on GitHub, not on this Mac.
MACOS_MARKER = r"(?<![\w/.-])mac ?os(?![\w/-]|\.\w)"
# Baby Bear the product, named in the TITLE (see TITLE_ONLY_LOCAL_MARKERS) and not as part of a skill,
# repo or path name (babybear-ship-and-ops, daveey/babybear, ~/code/babybear-h4): the BB repo is
# checked out on the hub and its devices are reached over ssh:mini, so a body that loads a BB skill
# (e.g. a metta disk check) is not Mac work. Mac-only BB work still hits xcode/simulator/codesign.
BABYBEAR_MARKER = r"(?<![\w/.~-])baby ?bear(?![\w/-]|\.\w)"
# Phrases that mean the work needs this machine (a Mac the user works on). Case-insensitive.
DEFAULT_LOCAL_MARKERS = (
    r"\bchrome\b", r"\bcdp\b", r"\bkeychain\b", r"\bcodesign", r"\bnotari[sz]", r"\btcc\b",
    r"\bosascript\b", r"\bapple ?script\b", r"\bapple (photos|notes|reminders|contacts|calendar|music)\b",
    r"\bphotos\.app\b", r"\bimessage\b", r"\bfind ?my\b", r"\bremindctl\b", r"\bshortcuts? app\b",
    r"\blaunchd\b", r"\blaunchctl\b", r"launchagents", r"~/library/", r"\bhermes\.app\b",
    r"\bhermes desktop\b", r"\bdesktop (app|backend|backends|renderer|window|chat|chats|sidebar|ui)\b",
    r"\bdesktop\s+dom\b",
    r"\bmacbook\b", MACOS_MARKER, r"\bmac ?mini\b", r"\bthe mini\b", r"\bdavids-mac-mini\b",
    BABYBEAR_MARKER, r"\bxcode\b", r"\bsimulator\b", r"\bwhatsapp\b", r"\bscreen ?shot",
    r"\bsoftmax-org\b", r"\bdavid-bloomin-sso\b", r"--profile softmax",
)
# Host names that mean "the work is on that machine" only when they are the subject of the
# title. A body mention is a repro or a verification step ("verify live on the hub and the
# MacBook"), which is not a reason to keep a core-code card on the laptop. A caller who replaces
# ``local_markers`` owns the list and every pattern scans the whole text.
TITLE_ONLY_LOCAL_MARKERS = frozenset({
    r"\bmacbook\b", r"\bmac ?mini\b", r"\bthe mini\b", r"\bdavids-mac-mini\b", BABYBEAR_MARKER,
})
# Product names (not hosts, not Apple tech): a product with Linux parts (BB pods, their CI on
# self-hosted Linux runners) is not an Apple/local need by name alone. With the built-in list, a
# product-name hit is skipped when the card names Linux and nothing Apple- or device-bound.
PRODUCT_NAME_MARKERS = frozenset({BABYBEAR_MARKER})
# Linux named outright: self-hosted runners, runs-on, docker and containers exist on Macs too.
LINUX_CONTEXT_MARKERS = (r"\blinux\b", r"\bubuntu\b")
APPLE_CONTEXT_MARKERS = (
    r"\bxcode", r"\bxcrun\b", r"\bfastlane\b", r"\bsimulator\b", r"\bcodesign", r"\bcode ?signing\b",
    r"\bsigning identit", r"\bdeveloper id\b", r"\bprovisioning\b", r"\bnotari[sz]", r"\btestflight\b",
    r"\bapp store\b", r"\bios\b", r"\biphone\b", r"\bipad\b", r"\bwatchos\b", r"\bswift(ui)?\b", r"\.app\b",
    r"\bcatalyst\b", r"\bcore ?ml\b", r"\bwatch app\b", r"\bav(foundation|audiosession)\b", r"\bapple\b",
    r"\bmac\b", MACOS_MARKER, r"\bmacbook\b", r"\bmac ?mini\b", r"\bthe mini\b", r"\bkeychain\b", r"\btcc\b",
)
LOCAL_MARKER_REMEDY = (
    "if the work doesn't need this machine, archive and re-create with placement='hub'")
PINNED_LOCAL_WHY = "placement pinned local by the caller"


def pinned_machine_why(machine: str) -> str:
    """Explain a machine pin without implying that the card executes on the creating gateway."""
    return f"placement pinned to machine {machine} (runs there through the Board lease)"


def local_pin_note(target: str) -> str:
    """Result note for a placement='local' pin whose card would run on ``target`` without it. [daveey]"""
    return ("placement='local' was pinned, but nothing in the card names a machine-only need; without the pin "
            f"it would run on {target}. If the work doesn't need this machine, archive this card and create it "
            "again without the pin.")

DEFAULT_LOCAL_SKILL_MARKERS = (
    r"^apple", r"^imessage$", r"^findmy$", r"^macos-", r"^mac-", r"^invisible-chrome-agent$",
    r"^local-chrome-remote-debugging$", r"^computer-use$", r"^babybear", r"^bb-", r"^ios-",
)
# Phrases that mean the card runs tests, builds or benchmarks (CPU/IO heavy): with ``heavy_route`` set it
# runs on a worker node rather than the routed target. Case-insensitive, title + body.
DEFAULT_HEAVY_MARKERS = (
    r"\bpytest\b", r"\brun_tests\.sh\b", r"\bvitest\b", r"\bnpm (run )?test\b", r"\btest suite\b",
    r"\b(regression|unit|integration|e2e) tests?\b", r"\bbench(mark)?(s|ing)?\b", r"\bbench-v\d",
    r"\bcargo (build|test)\b", r"\bdocker build\b", r"\bcompil(e|es|ing)\b", r"\buv sync\b",
    r"\bpip install\b", r"\bnpm (ci|install)\b", r"\bvenv\b", r"\bflaky\b",
)
# The routed target's own host named in the card: that work belongs on the target, not a worker.
TARGET_HOST_MARKERS = (r"\bhermes-hub\b", r"\bon the hub\b", r"\bthe hub's\b")
# target_live()'s reason for a pin hermes-hub has no gateway record of (a wrong name, not a dead gateway).
UNKNOWN_GATEWAY = "no gateway is named"
# kanban_create options hermes-hub create-task cannot carry: a card using them stays local.
UNCARRIED_OPTIONS = ("initial_status", "human_request", "workspace_path", "project", "project_id", "tenant",
                     "max_retries")  # hermes-hub create-task has no retry breaker option [daveey]
# kanban_create options a hermes-hub client with `create-task --option KEY=JSON` carries to the card.
# (hermes-hub also allows reasoning_effort, but kanban_create has no such parameter.)
CARRIED_OPTIONS = ("skills", "max_runtime_seconds", "priority", "model", "provider", "goal_mode",
                   "goal_max_turns", "completion_contract", "idempotency_key", "requires", "triage")
# Of those, the ones an older client (no --option) cannot express at all: the card stays local.
LEGACY_UNCARRIED = ("model", "provider", "goal_mode", "triage")
HELP_CACHE_SECONDS = 600
# Profile tools (``requires`` entries) a target can satisfy when it does not publish its profiles'
# tools: what the hub's default profile has. host_repo never travels: it means a checkout on THIS host.
DEFAULT_CARRIED_REQUIRES = ("terminal", "file", "git", "kanban")
_help_cache: dict = {}


class _Cfg:
    def __init__(self, raw: dict):
        self.raw = raw if isinstance(raw, dict) else {}

    def target(self, self_gw: Optional[str]) -> Optional[str]:
        route = self.raw.get("route")
        if not self_gw or not isinstance(route, dict):
            return None
        tgt = route.get(self_gw)
        return tgt if isinstance(tgt, str) and tgt and tgt != self_gw else None

    def markers(self) -> list[re.Pattern]:
        base = self.raw.get("local_markers")
        pats = list(base) if isinstance(base, list) else list(DEFAULT_LOCAL_MARKERS)
        extra = self.raw.get("extra_local_markers")
        if isinstance(extra, list):
            pats += extra
        out = []
        for p in pats:
            try:
                out.append(re.compile(str(p), re.IGNORECASE))
            except re.error:
                continue
        return out

    def skill_markers(self) -> list[re.Pattern]:
        base = self.raw.get("local_skill_markers")
        pats = list(base) if isinstance(base, list) else list(DEFAULT_LOCAL_SKILL_MARKERS)
        return [re.compile(str(p), re.IGNORECASE) for p in pats]

    def creator_session(self, target: str) -> str:
        v = self.raw.get("creator_session")
        if isinstance(v, dict):
            v = v.get(target)
        return v if isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}", v) \
            else DEFAULT_CREATOR_SESSION

    def live_seconds(self) -> int:
        try:
            return max(60, int(self.raw.get("hub_live_seconds", DEFAULT_HUB_LIVE_SECONDS)))
        except (TypeError, ValueError):
            return DEFAULT_HUB_LIVE_SECONDS

    def heavy_target(self, self_gw: Optional[str]) -> Optional[str]:
        route = self.raw.get("heavy_route")
        if not self_gw or not isinstance(route, dict):
            return None
        tgt = route.get(self_gw)
        return tgt if isinstance(tgt, str) and tgt and tgt != self_gw else None

    def heavy_markers(self) -> list[re.Pattern]:
        base = self.raw.get("heavy_markers")
        pats = list(base) if isinstance(base, list) else list(DEFAULT_HEAVY_MARKERS)
        out = []
        for p in pats:
            try:
                out.append(re.compile(str(p), re.IGNORECASE))
            except re.error:
                continue
        return out

    def assignees(self) -> set[str]:
        v = self.raw.get("assignees")
        return {str(x) for x in v} if isinstance(v, list) and v else {"default"}

    def carried_requires(self) -> set[str]:
        v = self.raw.get("carried_requires")
        return {str(x) for x in v} if isinstance(v, list) else set(DEFAULT_CARRIED_REQUIRES)


def target_info(target: str, *, now: float, remote: Optional[dict] = None) -> dict:
    """What ``target`` advertises through hermes-hub: ``{"profiles": [names] | None, "tools":
    {profile: {tool}} | None}``. ``tools`` is None when its capability record publishes no
    ``profile-tools:`` entry (an older core); only entries checked within the stale window count."""
    profiles = reg.remote_gateway_profiles(target)
    remote = reg.remote_records() if remote is None else remote
    tools: Optional[dict] = None
    limit = reg.stale_after()
    prefix = reg.PROFILE_TOOLS_NS + ":"
    for c in reg._entries(remote.get(target) or {}):
        name = c["name"]
        if not name.startswith(prefix):
            continue
        tools = {} if tools is None else tools
        raw = c.get("checked_at")
        if not c.get("ok") or isinstance(raw, bool) or not isinstance(raw, (int, float)) or now - raw > limit:
            continue
        _, prof, tool = name.split(":", 2)
        tools.setdefault(prof, set()).add(tool)
    return {"profiles": profiles, "tools": tools}


_round_config = threading.local()  # .cfg: the placement config pinned by ``config_snapshot`` on this thread


@contextmanager
def config_snapshot() -> Iterator[dict]:
    """Pin one placement-config load for this thread while the block runs (one sync round or links pass). [daveey]

    Placeholder checks run per task, subscription and link row, and each unpinned check reloaded the config: about 5k
    loads per chat-link pass on a 600-card board (2026-11-02). Nested snapshots share the outer one; another thread
    never sees it. The next block loads afresh, so a config edit applies from the next round.
    """
    pinned = getattr(_round_config, "cfg", None)
    if pinned is not None:
        yield pinned
        return
    _round_config.cfg = cfg = config()
    try:
        yield cfg
    finally:
        _round_config.cfg = None


def config() -> dict:
    pinned = getattr(_round_config, "cfg", None)
    if pinned is not None:
        return pinned
    try:
        from hermes_cli.config import load_config_readonly
        cfg = ((load_config_readonly() or {}).get("kanban") or {}).get("placement") or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def is_placeholder_session(value: str, *, placement_config: Optional[dict] = None) -> bool:
    """Placement attribution IDs must never become notification destinations.

    Batch callers may supply a per-pass snapshot to avoid reloading configuration per row.
    """
    session = str(value or "").rsplit("/", 1)[-1]
    if session.startswith(DEFAULT_CREATOR_SESSION):
        return True
    configured = (config() if placement_config is None else placement_config).get("creator_session")
    values = configured.values() if isinstance(configured, dict) else (configured,)
    return bool(session) and session in values


def _local_marker(title: str, body: str, skills, cfg: _Cfg) -> Optional[str]:
    """The first local-only hit, or None. Host-name markers (see
    :data:`TITLE_ONLY_LOCAL_MARKERS`) scan the title only, unless the caller replaced the list.
    A product name (:data:`PRODUCT_NAME_MARKERS`) does not pin a card whose text names Linux
    and no Apple or device-bound work (same exception: not with a replaced list)."""
    full = f"{title or ''}\n{body or ''}"
    replaced = isinstance(cfg.raw.get("local_markers"), list)
    linux_only = (not replaced
                  and any(re.search(p, full, re.IGNORECASE) for p in LINUX_CONTEXT_MARKERS)
                  and not any(re.search(p, full, re.IGNORECASE) for p in APPLE_CONTEXT_MARKERS))
    for rx in cfg.markers():
        if linux_only and rx.pattern in PRODUCT_NAME_MARKERS:
            continue
        text = (title or "") if (not replaced and rx.pattern in TITLE_ONLY_LOCAL_MARKERS) else full
        m = rx.search(text)
        if m:
            return f"mentions {m.group(0).lower()!r}"
    for s in skills or []:
        for rx in cfg.skill_markers():
            if rx.search(str(s)):
                return f"skill {s}"
    return None


def _heavy_marker(title: str, body: str, cfg: _Cfg) -> Optional[str]:
    """The first heavy-work hit, or None (also None when the card names the target's own host)."""
    full = f"{title or ''}\n{body or ''}"
    if any(re.search(p, full, re.IGNORECASE) for p in TARGET_HOST_MARKERS):
        return None
    for rx in cfg.heavy_markers():
        m = rx.search(full)
        if m:
            return f"mentions {m.group(0).lower()!r}"
    return None


# Work that only makes sense on the gateway that owns it (the hub, for a routeless gateway): its checkout and
# state, credentials broker, cron control plane, Kanban DB, Board/gateway admin. Used only for ``_routeless_heavy``.
#
# Repository paths are not markers: a worker provisions its own checkout (``~/code/<repo>``, a worktree of
# ``~/.hermes/hermes-agent``), so ordinary repo work is portable. Before 11-03 ``~/code/``, a bare ``~/.hermes``
# and the Board CLI name ``hermes-hub`` kept ~3 of every 4 ready hub cards off the worker pool, so the hub's
# local execution cap throttled the whole queue. What stays local: an existing worktree on this host
# (``~/code/_wt/<slug>``, ``~/code/_wt-remote/<slug>``, ``<repo>/.claude/worktrees/<x>``,
# ``~/code/<repo>/.worktrees/<x>``, ``~/.hermes/hermes-agent/.worktrees/<x>`` hold unpushed work), Hermes
# state under ``~/.hermes`` other than the code checkout (``~/.hermes/skills`` is host state: curator ledger,
# usage counts, live-patched skills), a
# ``hermes-hub <subcommand>`` run (it acts with this gateway's key and replica), and a card that declares
# ``requires: [host_repo]`` or a ``dir``/``worktree`` workspace.
HERMES_STATE_PATH = r"(?:~|/home/[^/\s]+)/\.hermes/(?!(?:hermes-agent|repos|bin)(?:/|\b))[\w.-]"
HOST_WORKTREE_PATH = r"(?:~|/home/[^/\s]+)/code/_wt(?:-[\w-]+)?(?:/|\b)"
# Any home-rooted ``<dir>/.worktrees`` (``~/code/<repo>/.worktrees/t_*``, ``~/.hermes/hermes-agent/.worktrees``):
# kanban worktrees there hold host-only commits and dirty files. ``.worktrees-notes.md`` and ``/tmp/.../.worktrees``
# are not matches.
LOCAL_WORKTREE_PATH = (r"/\.claude/worktrees\b|"
                       r"(?:~|/home/[^/\s]+)/(?:[^/\s`'\"]+/)*\.worktrees(?![\w-]|\.\w)")
HUB_CLI_COMMAND = (r"\bhermes-hub\s+(?:sync|status|sessions|inbox|tasks|where|links|requests?|inspect-legacy|"
                   r"reconcile-legacy|inspect-alias|retire-alias|adopt-task|create-task|depend|outcomes|"
                   r"install-watch|watch|diff-shared)\b")
HOME_GATEWAY_MARKERS = (
    HERMES_STATE_PATH, HOST_WORKTREE_PATH, LOCAL_WORKTREE_PATH, HUB_CLI_COMMAND,
    r"\bkanban\.db\b", r"\bcron ?(job|jobs|tab)?\b",
    r"\btoken-broker\b", r"\bhermes-sync\b", r"\bon the hub\b", r"\bthe hub's\b",
    r"\bgateway (restart|config|service)\b", r"\bsystemctl\b", r"\blaunchd\b", r"\bssh\b",
    r"\bjournalctl\b", r"\bthis host\b",
    r"\brestart(ing)? (the )?gateway\b", r"\bhermes config (set|edit|unset)\b",
)


# The note :func:`remote_body` appends when another gateway places a card here. It lists the origin's own
# host-only things (Chrome, Keychain, launchd, ssh) as what to block on, so scanning it as card text made
# every card a MacBook placed on the hub look Mac-bound, and none of them reached the worker pool.
_PLACEMENT_NOTE = re.compile(
    r"Placed on \S+ by \S+'s default placement rule \(kanban\.placement\):.*?don't park or ask David\.", re.DOTALL)


def without_placement_note(body: str) -> str:
    """``body`` minus the system-written placement note: it is routing advice, not the card's work."""
    return _PLACEMENT_NOTE.sub("", body or "")


# A producer fences quoted evidence (a proposal's "what happened on the hub", a log excerpt) with these markers so
# the host-need scan reads only the work text. Only complete pairs are removed: an unterminated or stray marker
# exempts nothing. The title is never fenced. Before this (kanban-12ee0e4b65) 233 of 259 self-heal proposals named
# the hub, its kanban.db, ssh or cron while describing the incident, so every fix card built from one stayed on
# the hub although the fix itself was a repo change landed by push. [daveey]
EVIDENCE_OPEN = "<!-- placement:evidence -->"
EVIDENCE_CLOSE = "<!-- /placement:evidence -->"
_EVIDENCE = re.compile(re.escape(EVIDENCE_OPEN) + r".*?" + re.escape(EVIDENCE_CLOSE), re.DOTALL)


def without_evidence(body: str) -> str:
    """``body`` minus every complete fenced evidence block (:data:`EVIDENCE_OPEN` ... :data:`EVIDENCE_CLOSE`)."""
    return _EVIDENCE.sub("", body or "")


def portable_reason(args: dict, cfg: Optional[dict] = None) -> Optional[str]:
    """Why a card cannot leave its origin; None means structurally portable, not admitted.

    Keep the conservative legacy routeless-heavy boundary, excluding only its heavy-marker
    requirement. Worker profile/tool/skill attestations and capacity are Board admission checks.
    """
    c = _Cfg(config() if cfg is None else cfg)
    if args.get("needs"):
        return "gateway-scoped need"
    if (args.get("workspace_kind") or "scratch") != "scratch":
        return "host workspace"
    board = args.get("board")
    if board and board != "default":
        return "non-default board"
    if (args.get("assignee") or "default") not in c.assignees():
        return "assignee not allowlisted"
    if any(args.get(k) not in (None, "", [], False) for k in UNCARRIED_OPTIONS):
        return "uncarried option"
    reqs = args.get("requires") or []
    reqs = [reqs] if isinstance(reqs, str) else list(reqs)
    if any(reg.is_gateway_need(r) for r in reqs):
        return "gateway-scoped requirement"
    if any(str(r) not in c.carried_requires() for r in reqs):
        return "uncarried requirement"
    title, body = args.get("title") or "", without_evidence(without_placement_note(args.get("body") or ""))
    if _local_marker(title, body, args.get("skills"), c):
        return "local marker"
    full = f"{title}\n{body}"
    extra = c.raw.get("extra_home_markers")
    pats = list(HOME_GATEWAY_MARKERS) + (list(extra) if isinstance(extra, list) else [])
    for p in pats:
        try:
            if re.search(p, full, re.IGNORECASE):
                return "home gateway marker"
        except re.error:
            continue
    return None


def _routeless_heavy(args: dict, c: _Cfg, self_gw: Optional[str],
                     machine_ok: Optional[Callable[[str], bool]], *, skip: bool = False) -> Optional[dict]:
    """Legacy heavy routing; worker pools reuse portability without changing this opt-out path."""
    worker = c.heavy_target(self_gw)
    if skip or not worker or portable_reason(args, c.raw):
        return None
    title, body = args.get("title") or "", args.get("body") or ""
    heavy = _heavy_marker(title, body, c)
    if not heavy or not (machine_ok or _machine_picks_up)(worker):
        return None
    return {"where": "local", "runs_on": [f"machine:{worker}"],
            "why": f"heavy work ({heavy}): runs on {worker} through the Board lease"}


def _machine_picks_up(name: str) -> bool:
    """``name`` is an active machine in the Board registry that is not held (draining) and has not turned
    pickup off. The Board registry does not publish ``pickup`` today (10-28: every entry lacked it, the hub
    and worker-1 included, though both claim ``runs_on`` cards), so a missing field counts as on:
    ``heavy_route`` naming the worker is the operator saying it picks up."""
    from hermes_cli import kanban_worker_node as wn
    entry = ((wn.registry() or {}).get("machines") or {}).get(name)
    if not entry or entry.get("state", "active") != "active" or entry.get("hold"):
        return False
    return "pickup" not in entry or wn.advertises_pickup(entry)


def target_live(target: str, *, now: float, live_seconds: int,
                meta: Optional[dict] = None, remote: Optional[dict] = None) -> Optional[str]:
    """None when ``target`` looks reachable through hermes-hub, else the reason it does not."""
    if meta is None:
        meta = _hub_meta()
    try:
        last_ok = float(meta.get("last_ok") or 0)
    except (TypeError, ValueError):
        last_ok = 0.0
    if now - last_ok > live_seconds:
        return "this gateway's hermes-hub sync has not succeeded recently"
    remote = reg.remote_records() if remote is None else remote
    rec = remote.get(target)
    if rec is None and remote:
        # A name hermes-hub has no record of is a wrong name, not an outage. "stale or missing" read as one, so
        # callers dropped the pin instead of fixing it: the Desktop calls the hub 'hub-ec2' and the mini
        # 'mac-mini' (22 refusals, 10-28..11-02). An empty registry can't tell the two apart. [daveey]
        return (f"{UNKNOWN_GATEWAY} {target!r}: hermes-hub knows {', '.join(sorted(remote))}. Pin one of those, "
                f"or map this name in kanban.placement.aliases")
    try:
        at = float((rec or {}).get("refreshed_at") or 0)
    except (TypeError, ValueError):
        at = 0.0
    if now - at > reg.stale_after():
        return f"{target}'s capability record is stale or missing"
    return None


def _hub_meta() -> dict:
    try:
        db = reg._ro(reg.hub_db_path())
    except Exception:
        return {}
    try:
        return {k: v for k, v in db.execute("SELECT k, v FROM meta")}
    except Exception:
        return {}
    finally:
        db.close()


def hub_carries_options(hub_cli: str) -> bool:
    """True when ``hermes-hub create-task --help`` lists ``--option`` (client 1.8.0+). Cached briefly:
    the client self-updates hourly."""
    hit = _help_cache.get(hub_cli)
    if hit and time.monotonic() - hit[0] < HELP_CACHE_SECONDS:
        return hit[1]
    rc, out, _ = hub_runner([hub_cli, "create-task", "--help"])
    ok = rc == 0 and "--option" in out
    _help_cache[hub_cli] = (time.monotonic(), ok)
    return ok


def hub_has_outcomes(hub_cli: str) -> bool:
    """True when the client has ``hermes-hub outcomes`` (1.9.1+): a one-second outcome fetch. Older
    clients only learn a request's result in a full ``sync`` (~30 s on a busy Mac). Cached like
    :func:`hub_carries_options`."""
    key = (hub_cli, "outcomes")
    hit = _help_cache.get(key)
    if hit and time.monotonic() - hit[0] < HELP_CACHE_SECONDS:
        return hit[1]
    rc, out, _ = hub_runner([hub_cli, "--help"])
    ok = rc == 0 and re.search(r"\boutcomes\b", out) is not None
    _help_cache[key] = (time.monotonic(), ok)
    return ok


def _hub_cli(c: _Cfg) -> str:
    return reg.hub_cli_path(c.raw.get("hub_cli"))


def _bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def carried_options(args: dict) -> dict:
    """The set :data:`CARRIED_OPTIONS` of a kanban_create call, as JSON types hermes-hub accepts.
    Defaults (False, 0, local-only, empty) are left out. Raises on a value kanban_create would refuse.
    ``idempotency_key`` is not sent: it already fixes the request id, which the hub dedupes on."""
    out: dict = {}
    for k in CARRIED_OPTIONS:
        v = args.get(k)
        if k == "idempotency_key":
            continue
        if v in (None, "", [], False):
            continue
        if k in ("goal_mode", "triage"):
            if _bool(v):
                out[k] = True
        elif k in ("max_runtime_seconds", "priority", "goal_max_turns"):
            n = int(v)
            if n or k != "priority":
                out[k] = n
        elif k in ("skills", "requires"):
            vals = [str(x) for x in ([v] if isinstance(v, str) else v) if str(x).strip()]
            if vals:
                out[k] = vals
        elif k == "completion_contract" and str(v).strip() == "local-only":
            continue
        else:
            out[k] = str(v)
    return out


def _uncarried(args: dict, c: _Cfg, carries: Optional[bool]) -> list[str]:
    """The options of ``args`` hermes-hub cannot carry. ``carries`` None probes the client, only
    when an option an older client lacks is actually used."""
    used = [k for k in UNCARRIED_OPTIONS if args.get(k) not in (None, "", False, [])]
    legacy = [k for k in LEGACY_UNCARRIED if args.get(k) not in (None, "", False, [])]
    if (args.get("completion_contract") or "local-only") != "local-only":
        legacy.append("completion_contract")
    if used or not legacy:
        return used
    if carries is None:
        carries = hub_carries_options(_hub_cli(c))
    return [] if carries else legacy


def canonical_pin(pin, self_gw: Optional[str] = None, cfg: Optional[dict] = None) -> str:
    """``pin`` lowercased, with a host-style alias resolved to its gateway name.

    The hub gateway is named ``hub`` but its machine is ``hermes-hub`` (EC2 host, the hermes-hub
    client, every doc and memory line), so models pin ``placement='hermes-hub'`` and got refused
    ("not a routed gateway (only 'hub')"). ``kanban.placement.aliases`` (``{alias: gateway}``) maps
    any name; built in, ``hermes-<gw>`` means ``<gw>`` when ``<gw>`` is this gateway or a routed one.
    Unknown names pass through unchanged (and are refused as before). Never raises.
    """
    try:
        p = str(pin or "").strip().lower()
        raw = config() if cfg is None else cfg
        raw = raw if isinstance(raw, dict) else {}
        aliases = raw.get("aliases")
        if isinstance(aliases, dict):
            hit = {str(k).strip().lower(): str(v).strip().lower() for k, v in aliases.items()}.get(p)
            if hit:
                return hit
        known = {str(self_gw or "").strip().lower()}
        route = raw.get("route")
        if isinstance(route, dict):
            known |= {str(k).strip().lower() for k in route} | {str(v).strip().lower() for v in route.values()}
        known.discard("")
        if p.startswith("hermes-") and p[len("hermes-"):] in known:
            return p[len("hermes-"):]
        return p
    except Exception:
        return str(pin or "").strip().lower()


def explicit_foreign_pin(args: dict, self_gw: Optional[str], cfg: Optional[dict] = None) -> str:
    """The gateway the caller pinned that is neither this gateway nor 'local'/'auto', else ''.
    Aliases resolve first (:func:`canonical_pin`)."""
    pin = canonical_pin(args.get("placement") or "auto", self_gw, cfg)
    if pin in ("", "auto", "local", "here", (self_gw or "").lower()):
        return ""
    return pin


def _raw_explicit_pin(args: dict) -> str:
    """Explicit pin when identity is unknown: not auto/local/here, else ''.

    Same fallback ``tools.kanban_tools._place_remote`` uses when ``self_gateway()`` itself
    fails. A name is not treated as this gateway — that needs a successful identity lookup.
    """
    try:
        pin = str(args.get("placement") or "").strip().lower()
    except Exception:
        return ""
    return "" if pin in ("", "auto", "local", "here") else pin


def decide(args: dict, *, origin_intent=None, now: Optional[float] = None, cfg: Optional[dict] = None,
           self_gw: Optional[str] = None, live: Optional[Callable[[str], Optional[str]]] = None,
           locate: Optional[Callable[[str], dict]] = None, carries: Optional[bool] = None,
           machine_ok: Optional[Callable[[str], bool]] = None,
           info: Optional[Callable[[str], dict]] = None) -> dict:
    """``_decide`` plus one rule: a caller who pinned another gateway never gets a silent local card.

    Any local outcome for such a pin (no route to that gateway, a structural limit, target not live)
    becomes ``{"where": "refuse", "why": ..., "pin": <gateway>}``, which ``kanban_create`` returns as a
    typed error. If identity lookup (or the pin check) raises before that rule runs, an explicit name
    other than auto/local/here is refused the same way; local/here and a pin already known to be this
    gateway stay local. Never raises.
    """
    known = ""
    try:
        sg = reg.self_gateway() if self_gw is None else self_gw
        known = sg.strip().lower() if isinstance(sg, str) else ""
        out = _decide(args, origin_intent=origin_intent, now=now, cfg=cfg, self_gw=sg, live=live,
                      locate=locate, carries=carries, machine_ok=machine_ok, info=info)
        pin = explicit_foreign_pin(args, sg, cfg)
        if pin and origin_intent is None and out.get("where") == "local" and not out.get("defer"):
            return {"where": "refuse", "why": out.get("why", ""), "pin": pin}
        return out
    except Exception as exc:
        why = f"placement check failed: {exc}"
        # Do not accept a foreign pin in this catch-all. Identity may have failed before we
        # could tell own-gateway from a name; only a pin we already resolved as ours stays local.
        if origin_intent is None:
            raw = _raw_explicit_pin(args)
            if raw and raw != known:
                return {"where": "refuse", "why": why, "pin": raw}
        return {"where": "local", "why": why}


def _decide(args: dict, *, origin_intent=None, now: Optional[float] = None, cfg: Optional[dict] = None,
            self_gw: Optional[str] = None, live: Optional[Callable[[str], Optional[str]]] = None,
            locate: Optional[Callable[[str], dict]] = None, carries: Optional[bool] = None,
            machine_ok: Optional[Callable[[str], bool]] = None,
            info: Optional[Callable[[str], dict]] = None) -> dict:
    """``{"where": "local"|<gateway>, "why": str}`` for a ``kanban_create`` call; heavy work bound for a
    worker is ``{"where": "local", "runs_on": [machine:<worker>], "why": ...}``. Never raises."""
    try:
        c = _Cfg(config() if cfg is None else cfg)
        self_gw = reg.self_gateway() if self_gw is None else self_gw
        pin = canonical_pin(args.get("placement") or "auto", self_gw, c.raw)
        from hermes_cli.kanban_worker_placement import creation_decision
        enrollment = creation_decision(args, c.raw, self_gw, pin, origin_intent)
        if enrollment is not None:
            return enrollment
        explicit = pin not in ("", "auto")
        target = c.target(self_gw)
        if explicit and (not target or pin != target.lower()):
            # An explicit pin to another gateway does not need a route entry for it: it takes the same
            # create_remote path as the routed target (same structural refusals below), and the
            # liveness check below refuses a gateway that is unknown or not live.
            target = pin
        if not target:
            # A gateway with no ``route`` entry (the hub) can still send portable heavy work to a worker node.
            return (_routeless_heavy(args, c, self_gw, machine_ok, skip=explicit or origin_intent is not None)
                    or {"where": "local", "why": "no placement route for this gateway"})
        if origin_intent is not None:
            return {"where": "local", "why": "courier creation for another gateway"}
        # Parents are local links, so the card is created here; with ``defer_parents`` (default on) it
        # still runs on the target: the dispatcher forwards it there once its parents finish
        # (:mod:`hermes_cli.kanban_placement_forward`). Every other rule still applies first.
        has_parents = bool(args.get("parents"))
        if has_parents and not _bool(c.raw.get("defer_parents", True)):
            return {"where": "local", "why": "has parents (dependencies are local links)"}
        if (args.get("workspace_kind") or "scratch") != "scratch":
            return {"where": "local", "why": f"{args.get('workspace_kind')} workspace"}
        board = args.get("board")
        if board and board != "default":
            return {"where": "local", "why": f"board {board!r}"}
        now = time.time() if now is None else now
        seen: dict = {}

        def tgt() -> dict:  # read the target's advertisement once, only when a rule needs it
            if "v" not in seen:
                try:
                    seen["v"] = (info or (lambda t: target_info(t, now=now)))(target) or {}
                except Exception:
                    seen["v"] = {}
            return seen["v"]

        assignee = args.get("assignee") or "default"
        if assignee not in c.assignees():
            profiles = tgt().get("profiles")
            if profiles is None:
                return {"where": "local", "why": f"assignee {assignee!r} is not placed remotely "
                                                 f"({target} advertises no profile list)"}
            if assignee not in profiles:
                return {"where": "local", "why": f"assignee {assignee!r} has no profile on {target}"}
        used = _uncarried(args, c, carries)
        if used:
            return {"where": "local", "why": f"uses {', '.join(used)} (not carried by hermes-hub create-task)"}
        reqs = args.get("requires") or []
        reqs = [reqs] if isinstance(reqs, str) else list(reqs)
        needs = [r for r in reqs if reg.is_gateway_need(r)]
        tools = [str(r) for r in reqs if not reg.is_gateway_need(r)]
        if tools:
            pinned = [r for r in tools if r not in c.carried_requires()]
            if pinned:
                return {"where": "local", "why": f"requires {', '.join(pinned)}, which {target} cannot "
                                                 "satisfy for this gateway (not in carried_requires)"}
            if not (carries if carries is not None else hub_carries_options(_hub_cli(c))):
                return {"where": "local", "why": f"requires {', '.join(tools)} (not carried by this "
                                                 "hermes-hub client's create-task)"}
            held = tgt().get("tools")
            if held is not None:
                lacking = [r for r in tools if r not in (held.get(assignee) or set())]
                if lacking:
                    return {"where": "local", "why": f"requires {', '.join(lacking)}, which {target}'s "
                                                     f"profile {assignee!r} lacks"}
        for need in needs:
            found = (locate or (lambda n: reg.locate(n, now=now)))(need)
            on_target = any(gw == target for gw, _ in found.get("holders") or [])
            if not on_target:
                return {"where": "local", "why": f"needs {need}, which {target} does not hold"}
        if not explicit:
            hit = _local_marker(args.get("title") or "", args.get("body") or "",
                                args.get("skills"), c)
            if hit:
                return {"where": "local", "why": f"local marker: {hit}"}
            worker = c.heavy_target(self_gw)
            heavy = _heavy_marker(args.get("title") or "", args.get("body") or "", c) if worker else None
            if heavy and (machine_ok or _machine_picks_up)(worker):
                return {"where": "local", "runs_on": [f"machine:{worker}"],
                        "why": f"heavy work ({heavy}): runs on {worker} through the Board lease"}
        if has_parents:
            # Liveness is checked when the card is forwarded, not now: its parents may run for hours.
            return {"where": "local", "defer": target,
                    "why": f"has parents: created here and forwarded to {target} once they finish"}
        dead = (live or (lambda t: target_live(t, now=now, live_seconds=c.live_seconds())))(target)
        if dead:
            return {"where": "local", "why": _target_not_live_why(target, dead)}
        return {"where": target, "why": "no local-only need" if not explicit else "placement pinned by the caller"}
    except Exception as exc:  # placement must never break card creation
        return {"where": "local", "why": f"placement check failed: {exc}"}


def _target_not_live_why(target: str, dead: str) -> str:
    from hermes_cli.kanban_capability_routing import has_replica
    if not has_replica():
        # A Board worker node runs no hermes-hub sync, so it never sees any gateway as live. Say so, with the
        # route that works from here (worker-1 run 10113 read "not live" as a dead MacBook). [daveey]
        dead = (f"this gateway has no hermes-hub replica (a Board worker node), so it cannot place cards on other "
                f"gateways. Use kanban_block(needs=[...]): a worker node relays the need to the card's home "
                f"gateway, which opens the owner card on {target}")
    if dead.startswith(UNKNOWN_GATEWAY):
        return dead  # a wrong name, not a dead gateway: "not live" sent callers to drop the pin
    return f"{target} not live: {dead}"


def _default_runner(argv: list[str], timeout: float = HUB_TIMEOUT) -> tuple[int, str, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, check=False, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)


def _default_spawn(argv: list[str]) -> None:
    """Start ``argv`` detached: it finishes (and records its result) even after the tool call returns."""
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         # detached-spawn: ok — the placement forwarder must outlive the tool call that starts it and record its result [daveey]
                         start_new_session=True)
    except OSError:
        pass


# Tests replace these with fakes that record argv.
hub_runner: Callable[[list[str]], tuple[int, str, str]] = _default_runner
hub_spawn: Callable[[list[str]], None] = _default_spawn


def request_id(self_gw: str, args: dict, origin_session: str, options: Optional[dict] = None) -> str:
    """Stable per (gateway, idempotency key | title+body+origin+options): exact retries reuse it."""
    fields = [args.get("title"), args.get("body"), args.get("assignee"), origin_session, args.get("skills")]
    extra = {k: v for k, v in (options or {}).items() if k not in ("skills", "idempotency_key")}
    if extra:  # only then, so a card without options keeps the id older clients computed
        fields.append(extra)
    key = args.get("idempotency_key") or json.dumps(fields, sort_keys=True)
    return f"placement:{self_gw}:{hashlib.sha256(str(key).encode()).hexdigest()[:24]}"


def remote_body(args: dict, *, self_gw: str, target: str, carried: bool = False) -> str:
    """The body sent to ``target``. ``carried`` (the client passes options as fields) drops the
    body-text fallback for skills and the runtime budget."""
    body = (args.get("body") or "").rstrip()
    extra = []
    skills = [] if carried else args.get("skills") or []
    if skills:
        extra.append(f"Skills to load first: {', '.join(str(s) for s in skills)}.")
    if args.get("max_runtime_seconds") and not carried:
        extra.append(f"Runtime budget asked by the creator: {int(args['max_runtime_seconds'])} s.")
    extra.append(
        f"Placed on {target} by {self_gw}'s default placement rule (kanban.placement): nothing in the card "
        f"named a {self_gw}-only need. If you hit something only {self_gw} has (its Chrome profile, Keychain, "
        f"Apple apps, launchd or Desktop state, AWS SSO profiles, ssh to the mini, a checkout this host "
        f"lacks), block with needs=['host:{self_gw}'] (or the exact capability, e.g. "
        f"'aws:<account>:admin', 'ssh:mini') so capability routing opens an owner card on {self_gw}; "
        "don't park or ask David.")
    return (body + "\n\n---\n" + "\n".join(extra)).strip()


def create_remote(args: dict, *, target: str, origin_session: Optional[str], origin_profile: str,
                  self_gw: str, cfg: Optional[dict] = None, hub_cli: str = "hermes-hub") -> dict:
    """Queue the card on ``target``. Returns ``{ok, request_id, rc, ...}``; ok False means create locally."""
    c = _Cfg(config() if cfg is None else cfg)
    carried = hub_carries_options(hub_cli)
    options = carried_options(args) if carried else {}
    rid = request_id(self_gw, args, origin_session or "", options)
    prior = remote_state(rid)
    if prior in ("queued", "sent", "done"):
        # An identical earlier call already sent this request: never send it again, return it as is.
        return {"ok": True, "request_id": rid, "queued_only": False, "retry": True}
    argv = [hub_cli, "create-task", target, str(args.get("title") or "untitled").strip()[:300],
            "--body", remote_body(args, self_gw=self_gw, target=target, carried=carried),
            "--assignee", str(args.get("assignee") or "default"), "--id", rid]
    for k, v in options.items():
        argv += ["--option", f"{k}={json.dumps(v)}"]
    if origin_session:
        argv += ["--origin-session", origin_session, "--origin-profile", origin_profile,
                 "--creator-session", c.creator_session(target)]
    else:
        argv += ["--origin-session", ""]
    rc, out, err = hub_runner(argv)
    if rc == 0:
        return {"ok": True, "request_id": rid, "queued_only": False}
    if rc == 3:  # hub unreachable after the liveness check: queued, sent on the next sync (durable)
        return {"ok": True, "request_id": rid, "queued_only": True}
    return {"ok": False, "request_id": rid, "rc": rc, "error": (err or out)[-400:]}


def _sent_row(rid: str) -> Optional[tuple]:
    try:
        db = reg._ro(reg.hub_db_path())
    except Exception:
        return None
    try:
        return db.execute("SELECT state, result FROM sent WHERE id = ?", (rid,)).fetchone()
    except Exception:
        return None
    finally:
        db.close()


def remote_state(rid: str) -> Optional[str]:
    """The request's state in this gateway's hermes-hub replica (local/queued/done/...), None if unknown."""
    row = _sent_row(rid)
    return row[0] if row else None


_OUTPUT_ID = re.compile(r'"id"\s*:\s*"(t_[A-Za-z0-9_]+)"')


def remote_task_id(rid: str) -> Optional[str]:
    """The target's task id once the request is done (None while pending/unknown)."""
    row = _sent_row(rid)
    if not row or row[0] != "done":
        return None
    try:
        res = json.loads(row[1] or "{}")
    except Exception:
        return None
    return _task_id_in(res)


def _task_id_in(res) -> Optional[str]:
    if not isinstance(res, dict):
        return None
    tid = res.get("task_id") or res.get("task")
    if isinstance(tid, str):
        return tid
    # A plain (unbound) create returns the owner's `hermes kanban create --json` text as "output",
    # truncated by the hub, so read the id with a pattern rather than a JSON parse.
    m = _OUTPUT_ID.search(res.get("output") or "") if isinstance(res.get("output"), str) else None
    return m.group(1) if m else None


def _owner_card(stdout: str) -> tuple[Optional[str], Optional[str]]:
    """(task id, status) from ``hermes-hub outcomes --id`` output when the hub already holds the owner's
    card: an origin-bound create stays ``waiting`` until this gateway's next full sync accepts the
    binding, but the card exists from the moment the owner posts it."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            out = json.loads(line)
        except ValueError:
            continue
        if isinstance(out, dict) and out.get("remote_state") in ("waiting", "done"):
            res = out.get("remote_result")
            st = res.get("status") if isinstance(res, dict) else None
            return _task_id_in(res), st if isinstance(st, str) else None
        return None, None
    return None, None


def remote_status(rid: str) -> Optional[str]:
    """The created card's status on the target (``ready``, ``triage``...) when its result names one."""
    row = _sent_row(rid)
    try:
        res = json.loads(row[1] or "{}") if row and row[0] == "done" else {}
    except Exception:
        return None
    st = res.get("status") if isinstance(res, dict) else None
    return st if isinstance(st, str) else None


def await_task_id(rid: str, *, hub_cli: str, budget: float, clock: Callable[[], float] = time.monotonic,
                  sleep: Callable[[float], None] = time.sleep) -> tuple[Optional[str], Optional[str]]:
    """Wait up to ``budget`` seconds for the target to make the card of request ``rid``.

    Returns (task id, status on the target), (None, None) while still pending.

    The result only reaches this gateway when its hermes-hub client fetches outcomes. A client with
    ``hermes-hub outcomes`` is asked about once a second; an older one gets ONE detached ``sync``
    (it outlives this wait, so a late result is still recorded) while the replica is polled."""
    tid = remote_task_id(rid)
    if tid or budget <= 0:
        return tid, remote_status(rid) if tid else None
    deadline = clock() + budget
    fast = hub_has_outcomes(hub_cli)
    if not fast:
        hub_spawn([hub_cli, "sync", "--quiet"])
    while True:
        if fast:
            left = max(1.0, min(5.0, deadline - clock()))
            argv = [hub_cli, "outcomes", "--id", rid, "--wait", f"{left:.1f}"]
            # Bound the call itself too (the client's HTTP timeout is 60 s); a test fake takes argv only.
            _, out, _ = (hub_runner(argv) if hub_runner is not _default_runner
                         else _default_runner(argv, timeout=left + 5))
            tid, st = _owner_card(out)
            if tid:
                return tid, st
        tid = remote_task_id(rid)
        if tid:
            return tid, remote_status(rid)
        if clock() >= deadline:
            return None, None
        sleep(1.0)
