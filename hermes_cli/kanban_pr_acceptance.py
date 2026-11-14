"""Exact-head GitHub acceptance for explicitly declared PR tasks.

Network work happens outside SQLite transactions. The lifecycle owner persists
receipts only after rechecking the captured run/status/contract under its lock.

``gh`` runs as the card's assignee profile (``profile_home``), not the ambient
login: :func:`_gh_env` resolves that profile's own credentials/config for the
subprocess — a multi-profile host's default ``gh`` login cannot read another
org's private repos (#122689).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

from hermes_cli.kanban_pr_merge_time import check_receipt, flag_main_health, merge_evidence, timestamp

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PR = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)")
# The way out of a contract that can never pass. A worker can't take it (orchestrator-only),
# so the worker blocks WITHOUT a human_request: the owning chat clears agent-owned parks,
# and David never gets a "approve closing this card" ask. [daveey]
_REPOINT_HINT = ("this contract can never pass: the orchestrator (not this card's worker, and not a human ask) "
                 "re-points it with `hermes kanban edit <task> --completion-contract <local-only|OWNER/REPO|PR URL> "
                 "--reason \"...\"`; a worker blocks without a human_request and names the evidence.")


def validate_contract(value: str | None) -> str:
    if value is None or value == "local-only":
        return "local-only"
    if not isinstance(value, str) or not (_REPO.fullmatch(value) or _PR.fullmatch(value)):
        raise ValueError("completion_contract must be local-only, OWNER/REPO, or an exact GitHub PR URL")
    return value


NO_REQUIRED_CHECKS_DETAIL = (
    "No repository-required checks are configured on {where}. All reported exact-head checks must pass; "
    "only when complete evidence is empty, and the work is verified another way, retry completion with "
    "metadata.contract_waiver={{\"reason\": \"<how it was verified>\"}} (honoured only while the repo has "
    "no required checks AND no reported CI; logged as a contract_waived event). Otherwise {repoint} Never edit "
    "completion_contract in kanban.db.")


def waiver_reason(metadata) -> str | None:
    """The stripped reason of ``metadata.contract_waiver`` (``{"reason": str}`` or a bare string), else None."""
    raw = metadata.get("contract_waiver") if isinstance(metadata, dict) else None
    if isinstance(raw, dict):
        raw = raw.get("reason")
    return raw.strip()[:500] if isinstance(raw, str) and raw.strip() else None


def _api(endpoint: str, *, query: str | None = None, paginate: bool = False,
         profile_home: str | None = None):
    from hermes_platform.resolver.github import resolve_gh
    env = _gh_env(profile_home)
    resolved = resolve_gh(env.get("PATH") if env is not None else None)
    if not resolved.found:
        raise _GateSetupError("gh executable not found; install GitHub CLI on PATH or in ~/bin")
    command = [*_gh_command_prefix(profile_home), *resolved.command,
               "api", endpoint, "--hostname", "github.com"]
    if query is not None:
        command += ["-f", "query=" + query]
    if paginate:
        command += ["--paginate", "--slurp"]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=30,
                                check=True, env=env)
    except subprocess.CalledProcessError as exc:
        # 401/403/404 = the login cannot see this repository (wrong profile identity
        # or missing grant), not a transient API failure. Persist only the status
        # code + endpoint, never gh's stderr (credentials/host details).
        denied = re.search(r"HTTP (40[134])", exc.stderr or "")
        if denied:
            raise _GateAuthError(f"HTTP {denied[1]} on {endpoint.split('?')[0]}") from None
        if exc.returncode == 4:  # gh's authentication-required exit: this profile has no login
            raise _GateAuthError(f"gh has no login for {endpoint.split('?')[0]}") from None
        # GraphQL NOT_FOUND makes gh exit 1 before the ``repository is None`` check below
        # can classify it: a repo this login can't see, or one that was renamed, moved or
        # suspended. Retrying never helps, so it is never ``infra``. [daveey]
        if "Could not resolve to a Repository" in (exc.stderr or ""):
            raise _GateAuthError(f"repository not found on {endpoint.split('?')[0]}") from None
        raise
    value = json.loads(result.stdout)
    if isinstance(value, dict) and value.get("errors"):
        raise ValueError("GitHub returned incomplete GraphQL evidence")
    return value


class _GateSetupError(RuntimeError):
    """The acceptance transport is not installed or configured correctly."""


class _GateAuthError(RuntimeError):
    """gh was refused at HTTP 401/403/404 (or GraphQL returned no repository):
    this profile's login cannot see the repo — an identity problem to fix, not
    an infrastructure blip to retry."""


def _gh_command_prefix(profile_home: str | None) -> list[str]:
    """Explicit per-assignee exec wrapper; secrets never return to this process.

    The wrapper receives the resolved gh argv and the already-isolated child env.
    This supports credential brokers without inheriting a supervisor's tokens.
    """
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.config_effective import load_profile_config_effective, load_user_config_effective
    from hermes_platform.resolver import locate_command
    from ruamel.yaml.error import YAMLError

    home = Path(profile_home) if profile_home else get_hermes_home()
    token = set_hermes_home_override(home)
    scope_token = None
    try:
        scope_token = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
        config = (load_profile_config_effective(home / "config.yaml", fail_closed=True)
                  if profile_home else load_user_config_effective(fail_closed=True))
        prefix = config.get("kanban", {}).get("pr_acceptance", {}).get("gh_command_prefix", [])
    except (OSError, ValueError, TypeError, AttributeError, YAMLError):
        # Parser/config errors can contain interpolated credentials. Persist no raw text.
        raise _GateSetupError("Cannot read kanban.pr_acceptance configuration; check profile config.yaml") from None
    finally:
        if scope_token is not None:
            reset_secret_scope(scope_token)
        reset_hermes_home_override(token)
    if not isinstance(prefix, list) or any(not isinstance(s, str) or not s or "\x00" in s for s in prefix):
        raise _GateSetupError("kanban.pr_acceptance.gh_command_prefix must be an argv list")
    if prefix:
        if not Path(prefix[0]).is_absolute() or not locate_command(prefix[0]).found:
            raise _GateSetupError("gh_command_prefix requires an installed absolute executable path")
    return prefix


def _gh_env(profile_home: str | None) -> dict[str, str] | None:
    """Child env for ``gh``: the card's profile identity when one is resolvable.

    The completion boundary runs in the worker (assignee), the CLI, or a
    reviewer/dispatcher turn, so an ambient ``gh`` login is whichever process
    happened to call it (#122689). ``served_profile_child_env(inherit_credentials=True)``
    is the seam for "this child acts for that profile": it scrubs the launch
    profile's credential residue and overlays the target profile's own
    ``GH_TOKEN``/``GH_CONFIG_DIR`` (its ``.env`` + external secret sources).
    ``None`` keeps the ambient env — unassigned cards behave exactly as before.
    """
    if not profile_home:
        return None
    from tools.environments.local import _is_routed_home, hermes_subprocess_env, served_profile_child_env
    from tools.environments.local_env_policy import _secret_source_bootstrap_env
    base = hermes_subprocess_env(inherit_credentials=True)
    routed = _is_routed_home(profile_home)
    if routed:
        # gh's config dir decides which login `gh api` uses, yet it is a path, not a
        # credential, so no scrub list sees it; the target's own value is overlaid from its .env.
        base.pop("GH_CONFIG_DIR", None)
    env = served_profile_child_env(base=base, target_home=profile_home, inherit_credentials=True)
    # The profile overlay re-adds a vault bootstrap token from the target's .env; gh never uses it.
    for name in _secret_source_bootstrap_env():
        env.pop(name, None)
    if routed and not (env.keys() & {"GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"}):
        # HOME/XDG_CONFIG_HOME are still the launch process's: without a login of its own the
        # child would fall through to ~/.config/gh/hosts.yml — the ambient login. Pin gh's config
        # to a profile-owned dir so it fails "not logged in" (exit 4 -> auth) instead.
        env["GH_CONFIG_DIR"] = str(Path(profile_home) / "gh")
    return env


def _assignee_profile_home(assignee: str | None) -> str | None:
    """Home whose ``gh`` login must read the contract repo — the assignee's, resolved
    exactly as the dispatcher resolves the worker's home — or None (unassigned) so the
    ambient login is used. An assigned card whose profile cannot be resolved is an
    identity failure (``auth``), never a silent fall-through to the ambient login."""
    if not assignee:
        return None
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    try:
        return resolve_profile_env(normalize_profile_name(assignee))
    except (FileNotFoundError, ValueError):
        raise _GateAuthError(f"assignee profile {assignee!r} cannot be resolved") from None


def collect_acceptance(contract: str, published_pr: str | None,
                       assignee: str | None = None) -> dict:
    receipt = {"ok": False, "classification": "missing", "head_sha": None,
               "pr_url": published_pr, "checks": [], "post_merge_checks": [],
               "recovery": "Fix required failures, rerun infrastructure checks or wait, then retry completion. "
                           "Use kanban_block if human input is needed; receipts remain on the task event log."}
    # Named in the catch-all detail so a failure says WHICH read broke, not "check gh auth". [daveey]
    phase = "resolving the assignee profile"
    try:
        profile_home = _assignee_profile_home(assignee)
        phase = "matching the published PR to the contract"
        declared = _PR.fullmatch(contract)
        url = contract if declared else published_pr
        match = _PR.fullmatch(url or "")
        if not match or (not declared and match[1] != contract) or (declared and published_pr and published_pr != contract):
            receipt.update(
                classification="contract_mismatch" if published_pr else "missing",
                completion_contract=contract,
                detail=f"Persisted completion contract: {contract}. "
                       f"metadata.published_pr: {published_pr or 'not supplied'}.",
                recovery="Retry completion with metadata.published_pr matching that contract "
                         "(an exact PR URL for a repository-only contract). A different PR belongs "
                         "in separate evidence, not in place of the bound PR. This is a metadata "
                         "error, not a CI failure; no acceptance checks have run.",
            )
            return receipt
        repo, number = match[1], int(match[2])
        receipt["pr_url"] = url
        owner, name = repo.split("/")
        phase = "reading the PR"
        query = (f'{{repository(owner:{json.dumps(owner)},name:{json.dumps(name)})'
                 f'{{pullRequest(number:{number:d})'
                 '{headRefOid baseRefName state mergedAt mergeCommit{oid} '
                 'baseRef{branchProtectionRule{requiredStatusChecks{context app{databaseId}}}}}}}')
        repository = _api("graphql", query=query, profile_home=profile_home)["data"]["repository"]
        if repository is None:
            # A private repo the login cannot read resolves to null, not an error.
            raise _GateAuthError(f"HTTP 404 on graphql {repo}")
        pr = repository["pullRequest"]
        sha, branch = pr["headRefOid"], pr["baseRefName"]
        receipt["head_sha"] = sha
        receipt["pr_state"] = pr["state"]
        if pr["state"] == "MERGED":
            receipt["recovery"] = (
                "Restore complete exact-head evidence from at or before the merge, then retry completion. "
                "Later main CI belongs to ci-main-watch; it cannot repair missing or failed pre-merge evidence.")
            timestamp(pr["mergedAt"])
        if pr["state"] == "CLOSED":
            # Closure is a disposition, not a claim that the change shipped.
            receipt.update(ok=True, classification="closed", detail="PR closed without merging; not delivery.")
            return receipt
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or pr["state"] not in {"OPEN", "MERGED"}:
            raise ValueError("PR head is unavailable")
        protection = (pr.get("baseRef") or {}).get("branchProtectionRule") or {}
        required = {(r["context"], (r.get("app") or {}).get("databaseId")) for r in protection.get("requiredStatusChecks", [])}
        phase = "reading branch rules"
        rules = _api(f"repos/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100",
                     paginate=True, profile_home=profile_home)
        for page in rules:
            for rule in page:
                if rule["type"] == "required_status_checks":
                    required.update((r["context"], r.get("integration_id"))
                                    for r in rule["parameters"]["required_status_checks"])
        receipt["required"] = [{"context": c, "app_id": a} for c, a in sorted(required, key=str)]

        phase = "reading check runs"
        # OPEN: GitHub's latest view (each suite's latest attempt), proven complete
        # against every suite's latest_check_runs_count. MERGED: every run, cut at merge.
        from hermes_cli.kanban_pr_check_runs import complete_runs, proven_latest_runs
        run_filter = "all" if pr["state"] == "MERGED" else "latest"
        pages = _api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter={run_filter}",
                     paginate=True, profile_home=profile_home)
        suites = []
        if run_filter == "latest" or not required:
            suites = _api(f"repos/{repo}/commits/{sha}/check-suites?per_page=100",
                          paginate=True, profile_home=profile_home)
        if run_filter == "latest":
            runs = proven_latest_runs(pages, suites, sha)
        else:
            runs = complete_runs(pages)
        phase = "reading commit statuses"
        from hermes_cli.kanban_pr_fallback import (actions_attempts, complete_statuses,
                                                   fallback_outcomes, mark_absence)
        statuses = complete_statuses(_api(f"repos/{repo}/commits/{sha}/statuses?per_page=100",
                                          paginate=True, profile_home=profile_home), sha)
        phase = "selecting merge-time evidence"
        runs, statuses = merge_evidence(receipt, pr, repo, runs, statuses, distinct_suites=not required,
                                        attempt_of=actions_attempts(_api, repo, profile_home))
        outcomes = []
        app_of_bot: dict[str, int | None] = {}
        for context, app_id in sorted(required, key=str):
            matching = [r for r in runs if r["name"] == context and
                        (app_id in (None, -1) or r["app"]["id"] == app_id)]
            # A legacy status satisfies an unpinned context, and a pinned one only when that
            # app posted it (GitHub does the same: Actions' status passes integration 15368).
            phase = "resolving status creators"
            legacy = [s for s in statuses if s["context"] == context and (
                app_id in (None, -1) or _status_app_id(s, app_of_bot, profile_home) == app_id)]
            selected = matching + ([max(legacy, key=lambda s: s["id"])] if legacy else [])
            if not selected:
                outcomes.append("missing")
                receipt["checks"].append({"name": context, "classification": "missing", "head_sha": sha})
            for check in selected:
                evidence = check_receipt(check, sha)
                outcomes.append(evidence["classification"])
                receipt["checks"].append(evidence)
        outcomes = fallback_outcomes(receipt, required, outcomes, runs, statuses, suites,
                                     merged=pr["state"] == "MERGED")
        # Failed empty suites joined post_merge_checks after merge_evidence; a no-op
        # unless the PR merged as this head, and the required path adds no rows.
        flag_main_health(receipt, pr, repo)
        # Re-read after all pages: old-head successes are never transferable.
        phase = "re-reading the PR head"
        current = _api(f"repos/{repo}/pulls/{number}", profile_home=profile_home)
        if current["head"]["sha"] != sha or current["base"]["ref"] != branch or (current["state"] == "closed" and not current.get("merged")):
            receipt.update(classification="stale", detail="PR head/base changed while collecting evidence; retry.")
            return receipt
        if current["state"] not in {"open", "closed"}:
            raise ValueError("PR state is unavailable")
        if bool(current.get("merged")) != (pr["state"] == "MERGED"):
            receipt.update(classification="stale", detail="PR merge state changed while collecting evidence; retry.")
            return receipt
        receipt["pr_state"] = "MERGED" if current.get("merged") else current["state"].upper()
        receipt["classification"] = next((x for x in outcomes if x != "success"), "missing" if not outcomes else "success")
        receipt["ok"] = receipt["classification"] == "success"
        mark_absence(receipt, required, outcomes, suites, f"{repo}@{branch}")
        return receipt
    except _GateSetupError as exc:
        receipt.update(classification="setup", detail=str(exc))
        return receipt
    except _GateAuthError as exc:
        login = f"assignee profile {assignee!r}'s gh login" if assignee else "the ambient gh login"
        receipt.update(classification="auth",
                       detail=f"GitHub refused the acceptance read ({exc}) as {login}; "
                              "fix that profile's GitHub credentials/access to the repository, then retry completion. "
                              f"If the repository was renamed, moved or suspended instead, {_REPOINT_HINT}")
        return receipt
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError) as exc:
        # Never persist gh stderr (credentials/host details); the failed phase is actionable.
        # Auth refusals are ``_GateAuthError`` above, so this text must not blame gh auth:
        # the respawn guard used to read "authentication" here as a provider auth wall. [daveey]
        receipt.update(classification="infra",
                       detail=f"GitHub acceptance evidence unavailable or incomplete while {phase} "
                              f"({type(exc).__name__}); retry completion.")
        return receipt


def _status_app_id(status: dict, cache: dict, profile_home: str | None) -> int | None:
    """The GitHub App that posted a legacy commit status, else None.

    The statuses API names only the creator user; an app posts as its bot user
    ``<slug>[bot]`` (type Bot), and ``apps/<slug>`` gives that app's id. Any other
    creator, or a slug GitHub doesn't know, is no app: the pinned context stays
    missing (fail closed). [daveey]"""
    creator = status.get("creator") or {}
    login = creator.get("login") or ""
    slug = login[:-len("[bot]")] if login.endswith("[bot]") else ""
    if creator.get("type") != "Bot" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", slug):
        return None
    if slug not in cache:
        try:
            cache[slug] = _api(f"apps/{slug}", profile_home=profile_home)["id"]
        except _GateAuthError:
            cache[slug] = None  # 404: a bot with no public app behind it
    return cache[slug]


def pr_closed_unmerged(url: str, assignee: str | None = None) -> bool:
    """True only when GitHub positively reports PR ``url`` as closed and not merged.
    Any error, or an open/merged PR, is False: a binding is replaced only on proof. [daveey]"""
    match = _PR.fullmatch(url or "")
    if not match:
        return False
    owner, name = match[1].split("/")
    query = (f'{{repository(owner:{json.dumps(owner)},name:{json.dumps(name)})'
             f'{{pullRequest(number:{int(match[2]):d}){{state}}}}}}')
    try:
        profile_home = _assignee_profile_home(assignee)
        repository = _api("graphql", query=query, profile_home=profile_home)["data"]["repository"]
        return bool(repository) and repository["pullRequest"]["state"] == "CLOSED"
    except Exception:
        return False


def _classify(check: dict, sha: str, outcome: str | None, is_run: bool) -> str:
    if check.get("head_sha", check.get("sha")) != sha:
        return "stale"
    if is_run and check.get("status") != "completed":
        return "pending"
    return {"success": "success", "failure": "failure", "error": "infra", "pending": "pending"}.get(outcome, "infra")
