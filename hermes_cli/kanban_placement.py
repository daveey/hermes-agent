"""Default Kanban placement: a card with no local-only need runs on another gateway. [daveey]

A gateway whose own machine must stay quiet (a laptop the user works on) maps itself to a target
gateway in ``kanban.placement.route`` (shared config, e.g. ``{"macbook": "hub"}``). ``kanban_create``
then asks :func:`decide` before writing a local card. Unless something pins the card here, it is
created on the target through ``hermes-hub create-task <gw> ... --origin-session <chat>``: the same
origin-bound path cross-gateway work already uses, so the card still shows in the chat that asked
for it, and it runs (and burns CPU) on the target.

A card stays local when any of these holds:

* the caller pinned it: ``placement="local"``;
* it cannot be expressed remotely: parents (dependencies are local links), a project, a ``dir`` or
  ``worktree`` workspace, a non-default board or assignee, courier (origin) creation, or creation
  options ``hermes-hub create-task`` cannot carry (model/provider pin, goal mode, triage, a non-local
  completion contract, an initial status, a human request);
* it declares a ``requires`` capability this gateway holds and the target does not;
* its title, body or skills match a local marker (``kanban.placement.local_markers`` replaces the
  built-in list, ``extra_local_markers`` extends it): Chrome, Keychain, TCC, Apple apps, launchd,
  the Desktop app, this Mac, the mini, AWS SSO profiles...;
* the target is not live: this gateway's hermes-hub sync has not succeeded recently, or the
  target's capability record is stale.

A worker on the target that still hits a local-only wall blocks with ``needs=[...]``; capability
routing (:mod:`hermes_cli.kanban_capability_routing`) then opens an owner card back here.

Unset ``route`` (upstream default) never places anything remotely.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from typing import Callable, Optional

from hermes_cli import kanban_capability_registry as reg

HUB_TIMEOUT = 60
DEFAULT_HUB_LIVE_SECONDS = 600
DEFAULT_CREATOR_SESSION = "kanban-placement"

# Phrases that mean the work needs this machine (a Mac the user works on). Case-insensitive.
DEFAULT_LOCAL_MARKERS = (
    r"\bchrome\b", r"\bcdp\b", r"\bkeychain\b", r"\bcodesign", r"\bnotari[sz]", r"\btcc\b",
    r"\bosascript\b", r"\bapple ?script\b", r"\bapple (photos|notes|reminders|contacts|calendar|music)\b",
    r"\bphotos\.app\b", r"\bimessage\b", r"\bfind ?my\b", r"\bremindctl\b", r"\bshortcuts? app\b",
    r"\blaunchd\b", r"\blaunchctl\b", r"launchagents", r"~/library/", r"\bhermes\.app\b",
    r"\bhermes desktop\b", r"\bdesktop (app|backend|backends|renderer|window|chat|chats|sidebar|ui)\b",
    r"\bmacbook\b", r"\bmac ?os\b", r"\bmac ?mini\b", r"\bthe mini\b", r"\bdavids-mac-mini\b",
    r"\bbaby ?bear\b", r"\bxcode\b", r"\bsimulator\b", r"\bwhatsapp\b", r"\bscreen ?shot",
    r"\bsoftmax-org\b", r"\bdavid-bloomin-sso\b", r"--profile softmax",
)
# Skills whose presence alone pins a card to a Mac.
DEFAULT_LOCAL_SKILL_MARKERS = (
    r"^apple", r"^imessage$", r"^findmy$", r"^macos-", r"^mac-", r"^invisible-chrome-agent$",
    r"^local-chrome-remote-debugging$", r"^computer-use$", r"^babybear", r"^bb-", r"^ios-",
)
# kanban_create options hermes-hub create-task cannot carry: a card using them stays local.
UNCARRIED_OPTIONS = ("model", "provider", "goal_mode", "triage", "initial_status", "human_request",
                     "workspace_path", "project", "project_id", "tenant")


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
                out.append(re.compile(str(p), re.I))
            except re.error:
                continue
        return out

    def skill_markers(self) -> list[re.Pattern]:
        base = self.raw.get("local_skill_markers")
        pats = list(base) if isinstance(base, list) else list(DEFAULT_LOCAL_SKILL_MARKERS)
        return [re.compile(str(p), re.I) for p in pats]

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

    def assignees(self) -> set[str]:
        v = self.raw.get("assignees")
        return {str(x) for x in v} if isinstance(v, list) and v else {"default"}


def config() -> dict:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = ((load_config_readonly() or {}).get("kanban") or {}).get("placement") or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _local_marker(text: str, skills, cfg: _Cfg) -> Optional[str]:
    for rx in cfg.markers():
        m = rx.search(text or "")
        if m:
            return f"mentions {m.group(0)!r}"
    for s in skills or []:
        for rx in cfg.skill_markers():
            if rx.search(str(s)):
                return f"skill {s}"
    return None


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


def decide(args: dict, *, origin_intent=None, now: Optional[float] = None, cfg: Optional[dict] = None,
           self_gw: Optional[str] = None, live: Optional[Callable[[str], Optional[str]]] = None,
           locate: Optional[Callable[[str], dict]] = None) -> dict:
    """``{"where": "local"|<gateway>, "why": str}`` for a ``kanban_create`` call. Never raises."""
    try:
        c = _Cfg(config() if cfg is None else cfg)
        if not isinstance(c.raw.get("route"), dict) or not c.raw["route"]:
            return {"where": "local", "why": "no placement route for this gateway"}
        self_gw = reg.self_gateway() if self_gw is None else self_gw
        target = c.target(self_gw)
        if not target:
            return {"where": "local", "why": "no placement route for this gateway"}
        pin = str(args.get("placement") or "auto").strip().lower()
        if pin in ("local", "here", (self_gw or "").lower()):
            return {"where": "local", "why": "placement pinned local by the caller"}
        explicit = pin not in ("", "auto")
        if explicit and pin != target.lower():
            return {"where": "local", "why": f"placement {pin!r} is not a routed gateway (only {target!r})"}
        if origin_intent is not None:
            return {"where": "local", "why": "courier creation for another gateway"}
        if args.get("parents"):
            return {"where": "local", "why": "has parents (dependencies are local links)"}
        if (args.get("workspace_kind") or "scratch") != "scratch":
            return {"where": "local", "why": f"{args.get('workspace_kind')} workspace"}
        board = args.get("board")
        if board and board != "default":
            return {"where": "local", "why": f"board {board!r}"}
        assignee = args.get("assignee") or "default"
        if assignee not in c.assignees():
            return {"where": "local", "why": f"assignee {assignee!r} is not placed remotely"}
        used = [k for k in UNCARRIED_OPTIONS if args.get(k) not in (None, "", False, [])]
        if (args.get("completion_contract") or "local-only") != "local-only":
            used.append("completion_contract")
        if used:
            return {"where": "local", "why": f"uses {', '.join(used)} (not carried by hermes-hub create-task)"}
        needs = [r for r in (args.get("requires") or []) if reg.is_gateway_need(r)]
        if [r for r in (args.get("requires") or []) if not reg.is_gateway_need(r)]:
            return {"where": "local", "why": "requires profile tools validated on this gateway"}
        now = time.time() if now is None else now
        for need in needs:
            found = (locate or (lambda n: reg.locate(n, now=now)))(need)
            on_target = any(gw == target for gw, _ in found.get("holders") or [])
            if not on_target:
                return {"where": "local", "why": f"needs {need}, which {target} does not hold"}
        if not explicit:
            hit = _local_marker(f"{args.get('title') or ''}\n{args.get('body') or ''}",
                                args.get("skills"), c)
            if hit:
                return {"where": "local", "why": f"local marker: {hit}"}
        dead = (live or (lambda t: target_live(t, now=now, live_seconds=c.live_seconds())))(target)
        if dead:
            return {"where": "local", "why": f"{target} not live: {dead}"}
        return {"where": target, "why": "no local-only need" if not explicit else "placement pinned by the caller"}
    except Exception as exc:  # placement must never break card creation
        return {"where": "local", "why": f"placement check failed: {exc}"}


def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=HUB_TIMEOUT, check=False, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)


# Tests replace this with a fake that records argv.
hub_runner: Callable[[list[str]], tuple[int, str, str]] = _default_runner


def request_id(self_gw: str, args: dict, origin_session: str) -> str:
    """Stable per (gateway, idempotency key | title+body+origin): exact retries reuse it."""
    key = args.get("idempotency_key") or json.dumps(
        [args.get("title"), args.get("body"), args.get("assignee"), origin_session, args.get("skills")],
        sort_keys=True)
    return f"placement:{self_gw}:{hashlib.sha256(str(key).encode()).hexdigest()[:24]}"


def remote_body(args: dict, *, self_gw: str, target: str) -> str:
    body = (args.get("body") or "").rstrip()
    extra = []
    skills = args.get("skills") or []
    if skills:
        extra.append(f"Skills to load first: {', '.join(str(s) for s in skills)}.")
    if args.get("max_runtime_seconds"):
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
    rid = request_id(self_gw, args, origin_session or "")
    argv = [hub_cli, "create-task", target, str(args.get("title") or "untitled").strip()[:300],
            "--body", remote_body(args, self_gw=self_gw, target=target),
            "--assignee", str(args.get("assignee") or "default"), "--id", rid]
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


def remote_task_id(rid: str) -> Optional[str]:
    """The target's task id once the request is done (None while pending/unknown)."""
    try:
        db = reg._ro(reg.hub_db_path())
    except Exception:
        return None
    try:
        row = db.execute("SELECT state, result FROM sent WHERE id = ?", (rid,)).fetchone()
        if not row or row[0] != "done":
            return None
        res = json.loads(row[1] or "{}")
        tid = res.get("task_id") or res.get("task") if isinstance(res, dict) else None
        return tid if isinstance(tid, str) else None
    except Exception:
        return None
    finally:
        db.close()
