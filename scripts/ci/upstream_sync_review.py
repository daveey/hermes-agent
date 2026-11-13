"""Which upstream syncs carry fork-authored content, and the independent review such a sync needs before it lands.

    python3 scripts/ci/upstream_sync.py --classify REV [--base REV]   # dry run: print the verdict, land nothing

A sync head is **mechanical** when every merge it brings that is not upstream's own has an empty
``git show --remerge-diff`` (git re-did the merge and got exactly the committed tree) and it brings no other
commit that upstream/main lacks. Anything else is **fork-authored**: a conflict resolution (a non-empty
remerge-diff) or a fork commit such as a ``[daveey] Upstream sync: ...`` fixup. That is code the fork wrote, and
the fork's rule for code is an independent exact-head approval plus green required CI (core PR #2479 landed 17
remerge-diff files and a test fixup with no review; t_01a1d6945ea46bd0).

The review gate is ``hermes-core-wt ship``'s own, loaded from ``hermes-core-wt-land`` (state repo; installed at
``~/.hermes/bin`` on the hub and ``~/.hermes/lib/core-tools`` on pool workers, or ``$HERMES_CORE_WT_LAND``): the
exact-head review card under its idempotency key ``core-wt-review:<repo>#<pr>:<sha>`` (filed with
``request_review``), then ``review_gate`` on the PR's exact head, then ``review_reply_guard`` for non-blocking
points. A host without that tool cannot judge a review, so a fork-authored sync is refused there, never landed.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

UPSTREAM_REMOTE = "upstream"
UPSTREAM_REF = "refs/remotes/upstream/main"
UPSTREAM_SLUG = "NousResearch/hermes-agent"  # the only repository whose commits count as upstream's own
LAND_ENV = "HERMES_CORE_WT_LAND"
_GITHUB_URL = re.compile(r"(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)([^/]+/[^/]+?)(?:\.git)?/?")


class ClassifyError(Exception):
    pass


def _git(repo, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=600, check=False)


def _out(repo, *args) -> str:
    proc = _git(repo, *args)
    if proc.returncode:
        raise ClassifyError(f"git {' '.join(args[:2])} failed: {(proc.stderr or proc.stdout).strip()[-300:]}")
    return proc.stdout


@dataclass
class Classification:
    head: str
    base: str
    upstream: str  # the upstream/main sha the range excluded (verified_upstream: fetched from the real upstream)
    resolutions: list = field(default_factory=list)  # [(merge sha, [files])]: non-empty remerge-diff
    commits: list = field(default_factory=list)  # [(sha, subject)]: non-merge commits upstream/main lacks
    clean_merges: list = field(default_factory=list)  # merges whose remerge-diff is empty

    @property
    def fork_authored(self) -> bool:
        return bool(self.resolutions or self.commits)

    @property
    def verdict(self) -> str:
        return "fork-authored" if self.fork_authored else "mechanical"

    def lines(self) -> list[str]:
        out = [f"merge {sha[:10]}: conflict resolution / fork edit (non-empty remerge-diff) in {len(files)} "
               f"file(s): {', '.join(files) or '?'}" for sha, files in self.resolutions]
        out += [f"commit {sha[:10]} {subject}" for sha, subject in self.commits]
        out += [f"merge {sha[:10]}: clean (empty remerge-diff)" for sha in self.clean_merges]
        return out

    def summary(self) -> str:
        files = sorted({f for _, fs in self.resolutions for f in fs})
        parts = []
        if self.resolutions:
            parts.append(f"{len(files)} remerge-diff file(s) in {len(self.resolutions)} merge(s)")
        if self.commits:
            parts.append(f"{len(self.commits)} fork commit(s): "
                         + ", ".join(f"{sha[:10]} {subject[:60]}" for sha, subject in self.commits[:5]))
        return "; ".join(parts) or "no fork-authored content"

    def review_scope(self) -> str:
        """What a reviewer of this sync reads: the fork-authored content only, not upstream's commits."""
        cmds = [f"git show --remerge-diff {sha}" for sha, _ in self.resolutions]
        cmds += [f"git show {sha}" for sha, _ in self.commits]
        return "\n".join(f"- `{c}`" for c in cmds)


_DIFF_PATH = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_CONFLICT_PATH = re.compile(r"^remerge CONFLICT \([^)]*\): .*? in (.+)$")


def remerge_files(repo, merge: str) -> list[str]:
    """Files whose committed content differs from git's own re-merge of ``merge``'s parents ([] = mechanical)."""
    text = _out(repo, "show", "--remerge-diff", "--no-color", "--no-ext-diff", "--format=", merge)
    if not text.strip():
        return []
    files = set()
    for line in text.splitlines():
        hit = _DIFF_PATH.match(line) or _CONFLICT_PATH.match(line)
        if hit:
            files.add(hit.groups()[-1])
    return sorted(files) or ["(unparsed remerge-diff)"]


def verified_upstream(repo) -> str:
    """upstream/main, fetched fresh from github.com/NousResearch/hermes-agent.

    Every commit upstream/main holds is excluded from review, so an ``upstream`` remote naming another repository
    (a fork holding a conflict-resolution merge), or a local ref moved onto fork commits, would hide fork-authored
    content and pass the sync as mechanical. The configured URL (read like ``origin_slug`` reads origin's) must
    name the real upstream, and the ref is re-fetched from it; anything else raises ClassifyError."""
    urls = _git(repo, "config", "--get-all", f"remote.{UPSTREAM_REMOTE}.url").stdout.split()
    wrong = [u for u in urls if not ((m := _GITHUB_URL.fullmatch(u)) and m[1].lower() == UPSTREAM_SLUG.lower())]
    if not urls or wrong:
        raise ClassifyError(f"the `{UPSTREAM_REMOTE}` remote is {', '.join(urls) or 'not configured'}, not "
                            f"github.com/{UPSTREAM_SLUG}: its commits cannot be trusted as upstream's own "
                            f"(git remote set-url {UPSTREAM_REMOTE} https://github.com/{UPSTREAM_SLUG}.git)")
    _out(repo, "fetch", "-q", "--no-tags", UPSTREAM_REMOTE, f"+refs/heads/main:{UPSTREAM_REF}")
    return _out(repo, "rev-parse", "--verify", f"{UPSTREAM_REF}^{{commit}}").strip()


def classify(repo, head: str, base: str) -> Classification:
    """Classify what ``head`` brings over ``base`` (the fork's main) that upstream/main does not already hold."""
    upstream = verified_upstream(repo)
    found = Classification(head, base, upstream)
    rng = [head, f"^{base}", f"^{upstream}"]
    for line in _out(repo, "log", "--reverse", "--format=%H%x09%P%x09%s", *rng, "--").splitlines():
        sha, parents, subject = (line.split("\t", 2) + ["", ""])[:3]
        parents = parents.split()
        if len(parents) > 2:
            found.resolutions.append((sha, ["(octopus merge: no remerge-diff)"]))
        elif len(parents) == 2:
            files = remerge_files(repo, sha)
            if files:
                found.resolutions.append((sha, files))
            else:
                found.clean_merges.append(sha)
        else:
            found.commits.append((sha, subject))
    return found


def landing_base(repo, head: str, main: str) -> str:
    """The base to classify ``head`` against: ``main`` while it is not landed; once landed, main just before the
    first-parent commit that brought it in (the dry run of an already-landed sync)."""
    if _git(repo, "merge-base", "--is-ancestor", head, main).returncode:
        return main
    chain = _out(repo, "rev-list", "--first-parent", "--ancestry-path", "--reverse", f"{head}..{main}").split()
    landing = chain[0] if chain else head
    return _out(repo, "rev-parse", f"{landing}^1").strip()


def land_tool_path() -> str:
    """hermes-core-wt-land on this host ("" when absent): $HERMES_CORE_WT_LAND, next to hermes-core-wt, or the
    hub/worker install directories."""
    env = os.environ.get(LAND_ENV)
    if env:
        return env if Path(env).is_file() else ""
    home = Path.home()
    tried = []
    wrapper = shutil.which("hermes-core-wt")
    if wrapper:
        tried.append(Path(wrapper).resolve().parent / "hermes-core-wt-land")
    tried += [home / ".hermes/bin/hermes-core-wt-land", home / ".hermes/lib/core-tools/hermes-core-wt-land"]
    return next((str(p) for p in tried if p.is_file()), "")


def load_land_tool(path: str):
    """Import hermes-core-wt-land (an extensionless script) as a module, without running its main."""
    loader = importlib.machinery.SourceFileLoader("hermes_core_wt_land_for_sync", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise ImportError(f"no import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module  # dataclasses and pickling resolve the module by name
    loader.exec_module(module)
    return module


class ReviewUnavailable(Exception):
    """The review gate cannot be judged on this host: refuse, never land."""


# The ship-gate entry points this gate calls, with the parameters it passes: an installed tool that drifted
# from them refuses here instead of failing mid-judgement (or, worse, judging with a changed meaning).
SHIP_GATE_CONTRACT = {
    "keyed_review_card": ("slug", "number", "sha"),
    "request_review": ("wt", "slug", "number", "sha", "old", "base", "prior_review", "impl_task", "gh"),
    "review_gate": ("gh", "slug", "pr", "sha", "branch", "base", "review_task", "impl_task"),
    "review_reply_guard": ("gh", "slug", "pr", "review", "reply"),
}


def contract_errors(mod) -> list[str]:
    errors = [f"{name} missing" for name in ("Gh", "ReviewRefused", "ReviewCIStopped") if not hasattr(mod, name)]
    for name, params in SHIP_GATE_CONTRACT.items():
        fn = getattr(mod, name, None)
        if not callable(fn):
            errors.append(f"{name} missing")
            continue
        have = inspect.signature(fn).parameters
        errors += [f"{name} lacks {p}" for p in params if p not in have]
    return errors


class _NeverRaised(Exception):
    """Stands in for a ship-tool exception class an older tool lacks."""


class ShipReview:
    """``hermes-core-wt ship``'s independent exact-head review gate, applied to a sync PR.

    ``check`` returns (approved, message). Not approved: the message names the review card (or the exact
    ``kanban_create`` a worker shell must submit) and what lands it. Raises ReviewUnavailable when the gate
    cannot be read (no ship tool, a failed card filing, an unreadable PR): the caller refuses."""

    def __init__(self, repo, *, reply: str = "", impl_task: str = "", tool: str | None = None):
        self.repo, self.reply, self.tool, self.impl_task = str(repo), reply, tool, impl_task

    def _module(self):
        path = self.tool or land_tool_path()
        if not path:
            raise ReviewUnavailable("review gate unavailable: hermes-core-wt-land is not installed on this host "
                                    f"(set ${LAND_ENV}); a fork-authored sync cannot be judged here")
        try:
            mod = load_land_tool(path)
        except Exception as exc:  # health: allow BLE001 -- any load fault of the ship tool refuses, named
            raise ReviewUnavailable(f"review gate unavailable: cannot load {path}: {exc!r}") from exc
        errors = contract_errors(mod)
        if errors:
            raise ReviewUnavailable(f"review gate unavailable: {path} does not offer ship's review entry points "
                                    f"this gate calls ({'; '.join(errors)})")
        return mod

    def check(self, gh_repo: str, number: int, head: str, branch: str) -> tuple[bool, str]:
        mod = self._module()
        quota_brake = getattr(mod, "SharedQuotaBrake", _NeverRaised)
        try:
            return self._check(mod, gh_repo, number, head, branch)
        except mod.ReviewCIStopped as exc:
            raise ReviewUnavailable(f"review card not filed: {exc}") from exc
        except quota_brake as exc:
            return False, f"GitHub quota brake ({exc}); run again later"
        except ReviewUnavailable:
            raise
        except Exception as exc:  # health: allow BLE001 -- any other ship-tool fault refuses, named, never a traceback
            raise ReviewUnavailable(f"review gate failed: {type(exc).__name__}: {exc}") from exc

    def _check(self, mod, gh_repo, number, head, branch) -> tuple[bool, str]:
        gh = mod.Gh(self.repo)
        pr = {"number": number}
        card = mod.keyed_review_card(gh_repo, number, head)
        try:
            receipt = mod.review_gate(gh, gh_repo, pr, head, branch, "main", card, impl_task=self.impl_task)
        except mod.ReviewRefused as exc:
            refusal = str(exc)
        else:
            code, why = mod.review_reply_guard(gh, gh_repo, pr, receipt, self.reply)
            if code is not None:
                return False, (f"{why} (answer with `upstream_sync.py --head {head[:12]} --review-reply "
                               "\"<answers>\"`)")
            who = receipt.get("id") or (receipt.get("reviewer") or {}).get("login") or "?"
            return True, f"independent exact-head review {receipt.get('source', '?')} {who} approves {head}"
        if not card:
            card, why, action = mod.request_review(self.repo, gh_repo, number, head, "", "main", "", self.impl_task,
                                                   gh=gh)
            if not card:
                if action:
                    return False, (f"file the exact-head review card with kanban_create ({why}), link it as this "
                                   f"card's parent, park, then rerun: {json.dumps(action, sort_keys=True)}")
                raise ReviewUnavailable(f"review card not filed: {why}")
        return False, (f"waiting for independent exact-head review card {card} of {head} ({refusal}); rerun "
                       f"`python3 scripts/ci/upstream_sync.py --head {head[:12]}` from the session that owns this "
                       "sync once it approves")
