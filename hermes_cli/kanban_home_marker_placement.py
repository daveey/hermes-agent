"""Home-gateway markers on a pool worker: queue for the pool unless this worker holds the card's own state. [daveey]

A home-gateway marker (:data:`hermes_cli.kanban_placement.HOME_GATEWAY_MARKERS`) keeps a card on the gateway that
creates it, because the text names something only that gateway has. On a pool worker that was rarely true: in the
24 h to 11-13 01:06Z, 473 worker-created cards hit one and only their creating worker could claim them (workers pick
up only ``runs_on`` cards), while every pool worker reaches hub and fleet state with ``hub-admin``
(``capability:hub-write``) and carries ssh and token-broker. t_01a1d2856e5fec44 classifies each hit
(:data:`~hermes_cli.kanban_placement.HOME_MARKER_CLASSES`): the card stays only when this worker holds its unpublished
state (a named tree here that is dirty or has unpushed commits, a named Kanban/scratch artifact that exists here),
when it says 'this host' (the creating worker), or for an operator ``extra_home_markers`` pattern. Every other hit
queues it, with :data:`~hermes_cli.kanban_placement.HOME_CLASS_LABELS`.

The probes are bounded and fail toward keep: at most :data:`MAX_TREES` trees under one :data:`PROBE_BUDGET_S`
budget, ``ls-remote`` only (no fetch), and one ``exists()`` per named artifact. They run only from
:func:`hermes_cli.kanban_worker_placement.queue_decision` on a pool worker; ``portability()`` stays probe-free. The
result is kept for the rest of the create (:func:`recorded`) so the created event's ``home_marker`` audit reports the
decision that placed the card, without probing again.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

PROBE_BUDGET_S = 5.0
MAX_TREES = 5
_HOME = r"(?:~|/home/[^/\s]+)"
# Whole worktree roots only: a subpath names the same tree.
_WT_ROOT = re.compile(_HOME + r"/code/_wt(?:-[\w-]+)?/[\w-]+(?:\.[\w-]+)*")
_DOT_WT_ROOT = re.compile(_HOME + r"/(?:[^/\s`'\"]+/)*\.worktrees/[\w-]+(?:\.[\w-]+)*")
_CLAUDE_WT_ROOT = re.compile(r"(?:~|/)[^\s`'\"]*/\.claude/worktrees/[\w-]+(?:\.[\w-]+)*")
_STATE = re.compile(_HOME + r"/\.hermes/[^\s`'\"<>()\[\]]*")
_REL_STATE = re.compile(r"\.hermes/[^\s`'\"<>()\[\]]*", re.IGNORECASE)
# This worker's own unpublished files: a card's workspace, evidence or attachments, or a scratch file.
_ARTIFACT = re.compile(r"/\.hermes/(kanban/(?:workspaces|evidence|attachments)|cache/scratch)/([^/\s]+)")

KEEP_TAIL = ("Push the branch, or attach the files with kanban_attach and drop the path, to let any pool worker "
             "run it.")
NOTES = {
    "b": ("{hit} names hub or fleet state; every pool worker reaches it with hub-admin (capability:hub-write), so "
          "this card joins the pool instead of waiting for {gateway}."),
    "c": "{hit} is Mac work; it joins the pool with capability:tailnet-exec, so the run reaches the Mac with "
         "tailnet_exec/ssh.",
    "d": "{hit} is a tool every pool worker carries.",
    "path": "{hit} is not unpublished state on {gateway} (absent here, or clean and pushed).",
}


def _local_path(raw: str) -> Path:
    """``raw`` on this host: ``~`` and ``/home/<this user>`` expand against ``Path.home()``."""
    if raw.startswith("~/"):
        return Path.home() / raw[2:]
    m = re.match(r"/home/([^/]+)/(.*)", raw)
    if m and m.group(1) == Path.home().name:
        return Path.home() / m.group(2)
    return Path(raw)


def _clean(raw: str) -> str:
    return raw.rstrip(".,;:")


class _Probe:
    """One create's probe budget: :data:`MAX_TREES` trees under :data:`PROBE_BUDGET_S` seconds."""

    def __init__(self):
        self.deadline = time.monotonic() + PROBE_BUDGET_S
        self.trees = 0

    def tree(self, raw: str):
        """``(keep evidence or None)`` for the worktree root ``raw``; None means it may queue."""
        from hermes_cli import kanban_move_pool as kmp
        path = _local_path(raw)
        if not path.exists():
            return None
        remaining = self.deadline - time.monotonic()
        if self.trees >= MAX_TREES or remaining <= 0:
            return f"{path} exists here and was not checked in the probe budget"
        self.trees += 1
        if not kmp._is_tree(path):
            return f"{path} exists only on this worker"
        try:
            return kmp.tree_state([str(path)], refresh=False, budget=remaining)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return f"{path} could not be checked ({type(exc).__name__})"


def _operator(pattern, match, gateway, probe):
    # an operator extra_home_markers pattern (or an unclassified built-in) fails toward keep
    yield "keep", "operator", match.group(0), (), "operator marker"


def _this_host(pattern, match, gateway, probe):
    yield "keep", "a-this-host", match.group(0), (), "'this host' names this worker"


def _reach(cls):
    """Classes (b), (c), (d): queue with the class's reach label, if any."""
    def verdict(pattern, match, gateway, probe):
        from hermes_cli import kanban_placement as kp
        hit = match.group(0)
        label = kp.HOME_CLASS_LABELS.get(cls)
        yield "queue", cls, hit, (label,) if label else (), NOTES[cls].format(hit=repr(hit), gateway=gateway)
    return verdict


def _worktree(pattern, match, gateway, probe):
    from hermes_cli import kanban_placement as kp
    roots = _WT_ROOT if pattern == kp.HOST_WORKTREE_PATH else None
    found = ([_clean(m.group(0)) for m in roots.finditer(match.string)] if roots else
             [_clean(m.group(0)) for rx in (_DOT_WT_ROOT, _CLAUDE_WT_ROOT) for m in rx.finditer(match.string)])
    for root in dict.fromkeys(found) or [match.group(0)]:
        state = probe.tree(root) if root in found else None
        if state:
            yield "keep", "a-worktree", root, (), state
        else:
            yield "queue", "worktree-not-here", root, (), NOTES["path"].format(hit=root, gateway=gateway)


def _state_path(pattern, match, gateway, probe):
    from hermes_cli import kanban_placement as kp
    # ``$HOME``/``${HOME}`` prefixes too (kp.HOME_PREFIX). A relative path after a cd home is named as its ``~/``
    # path, not the whole cd..path span the hit covers (review 5895876398).
    state = re.compile(kp.HOME_PREFIX + _STATE.pattern[len(_HOME):])
    paths = [p for p in (_clean(m.group(0)) for m in state.finditer(match.string))
             if re.match(kp.HERMES_STATE_PATH, p)]
    rel = _REL_STATE.match(match.string, match.start() + match.group(0).lower().rfind(".hermes/"))
    for raw in dict.fromkeys(paths) or [_clean("~/" + rel.group(0)) if rel else match.group(0)]:
        if art := _ARTIFACT.search(raw):
            # health: allow HX001 -- the card text names ~/.hermes literally; probe that same path on this worker
            if (Path.home() / ".hermes" / art.group(1) / art.group(2)).exists():
                yield "keep", "a-artifact", raw, (), f"{raw} exists only on this worker"
            else:
                yield ("queue", "artifact-not-here", raw, (kp.HOME_CLASS_LABELS["b"],),
                       NOTES["path"].format(hit=raw, gateway=gateway))
        else:
            yield ("queue", "b", raw, (kp.HOME_CLASS_LABELS["b"],),
                   NOTES["b"].format(hit=repr(raw), gateway=gateway))


_VERDICTS = {"a": _this_host, "b": _reach("b"), "c": _reach("c"), "d": _reach("d"),
             "worktree": _worktree, "state-path": _state_path}


def _verdicts(pattern: str, match: re.Match, gateway: str, probe: _Probe):
    """Yield ``(decision, class, hit, labels, sentence-or-evidence)`` for one hit."""
    from hermes_cli import kanban_placement as kp
    handler = _VERDICTS.get(kp.HOME_MARKER_CLASSES.get(pattern) or "", _operator)
    yield from handler(pattern, match, gateway, probe)


def classify(args: dict, cfg, gateway: str):
    """``{decision, labels, note, class, hit}`` for the card's home-gateway-marker hits on pool worker ``gateway``,
    or None when it has none. Any keep hit keeps the card; it queues only when every hit queues, with the union of
    their labels and one note sentence per class. Reused for the rest of the create. [daveey]"""
    from hermes_cli.kanban_placement_diagnostics import home_marker_hits
    cache = _cache()
    key = (gateway, *_key(args))
    if cache is not None and key in cache:
        return cache[key]
    hits = home_marker_hits(args, cfg)
    result = None
    if hits:
        probe = _Probe()
        verdicts = [v for pattern, match in hits for v in _verdicts(pattern, match, gateway, probe)]
        keeps = [v for v in verdicts if v[0] == "keep"]
        if keeps:
            _, cls, hit, _, evidence = keeps[0]
            result = {"decision": "keep", "labels": set(), "class": cls, "hit": hit,
                      "note": f"kept on {gateway}: {evidence}. {KEEP_TAIL}"}
        else:
            notes = {}
            for _, cls, _, _, sentence in verdicts:
                notes.setdefault(cls, sentence)
            result = {"decision": "queue", "labels": {label for v in verdicts for label in v[3]},
                      "class": verdicts[0][1], "hit": verdicts[0][2], "note": " ".join(notes.values())}
    if cache is not None:
        cache[key] = result
        if result is not None:
            remember(args, {"decision": result["decision"], "class": result["class"], "hit": result["hit"]})
    return result


def _key(args: dict):
    return str(args.get("title") or ""), str(args.get("body") or "")


def _cache():
    from hermes_cli.kanban_published_worktree import create_observation
    obs = create_observation()
    return None if obs is None else obs.setdefault("home_marker_classify", {})


def remember(args: dict, record: dict) -> None:
    """Keep the ``home_marker`` audit record for this create (no-op outside a create's observation)."""
    from hermes_cli.kanban_published_worktree import create_observation
    obs = create_observation()
    if obs is not None:
        obs.setdefault("home_marker", {})[_key(args)] = record


def recorded(args: dict):
    """The ``home_marker`` audit record this create's placement wrote for ``args``, or None."""
    from hermes_cli.kanban_published_worktree import create_observation
    return ((create_observation() or {}).get("home_marker") or {}).get(_key(args))


def unclaimable(args: dict) -> None:
    """The queue result found no live machine holding its whole label set, so the local keep stands: record that as
    ``gateway-test-failed`` (no live label), not as the queue it would have been. A keep never reaches here."""
    if (record := recorded(args)) is not None:
        remember(args, {"decision": "keep", "class": "gateway-test-failed", "hit": record["hit"]})


HOME_WHY = " (home gateway marker {hit} not bound to {gateway})"


def pool_worker_markers(args: dict, cfg, gateway: str, reason: str, live):
    """What a pool worker does with a card whose ``portable_reason`` is ``reason`` (a local or home-gateway marker).

    None leaves the legacy keep: the gateway test failed (not a Linux non-hub Board machine, or ``live()`` finds no
    unheld machine with the pool label), another portability reason keeps the card, or an operator local marker
    hit. ``{"keep": <decision>}`` keeps it here with a ``note`` naming what this worker holds. Otherwise
    ``{labels, note, why}``: the card queues with these extra labels (t_01a1d2662553185d local markers,
    t_01a1d2856e5fec44 home markers). [daveey]"""
    from hermes_cli import kanban_placement as kp
    from hermes_cli import kanban_worker_placement as kwp
    from hermes_cli.kanban_placement_diagnostics import home_marker_hits, portability

    if not kp.linux_pool_gateway(gateway, cfg) or not live():
        if reason == "home gateway marker" and not _is_hub(gateway, cfg) and (hits := home_marker_hits(args, cfg)):
            remember(args, {"decision": "keep", "class": "gateway-test-failed", "hit": hits[0][1].group(0)})
        return None
    if portability(args, cfg, local_markers=False, home_markers=False)[0]:
        return None
    local = kwp.unmet_markers(args, cfg)
    if reason == "local marker" and local is None:
        return None  # an operator local marker
    home = classify(args, cfg, gateway)
    if home is not None and home["decision"] == "keep":
        return {"keep": {"where": "local", "why": f"home gateway marker: mentions {home['hit']!r}",
                         "note": home["note"]}}
    labels, notes, why = set(), [], ""
    if local is not None:
        labels |= local["labels"]
        notes.append(local["note"])
        why += kwp.UNMET_WHY.format(hit=local["hit"], gateway=gateway)
    if home is not None:
        labels |= home["labels"]
        notes.append(home["note"])
        why += HOME_WHY.format(hit=repr(home["hit"]), gateway=gateway)
    return {"labels": labels, "note": " ".join(notes), "why": why}


def _is_hub(gateway, cfg) -> bool:
    from hermes_cli.kanban_hub_admission_placement import is_hub
    return bool(gateway) and is_hub(gateway, cfg or {})
