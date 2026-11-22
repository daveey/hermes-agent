"""REST projection of the PR/policy evidence used by completion acceptance."""
from urllib.parse import quote


class GraphQLRateLimit(ValueError):
    """Only GitHub GraphQL quota exhaustion; not a local budget or auth refusal."""


class UnprotectedBranch(ValueError):
    """GitHub positively reports that the base branch is not protected."""


def quota_error(text: str) -> bool:
    lowered = text.lower()
    return "rate_limited" in lowered or "api rate limit" in lowered


def read_pr(api, repo: str, number: int, profile_home: str | None) -> dict:
    """Preserve policy app IDs and merge timestamps; unreadable policy fails closed."""
    pull = api(f"repos/{repo}/pulls/{number}", profile_home=profile_home)
    state, merged = pull["state"], pull["merged"]
    if state not in {"open", "closed"} or not isinstance(merged, bool) or (merged and state != "closed"):
        raise ValueError("Invalid REST PR state")
    result = {"headRefOid": pull["head"]["sha"], "baseRefName": pull["base"]["ref"],
              "state": "MERGED" if merged else state.upper(),
              "mergedAt": pull["merged_at"], "mergeCommit": {"oid": pull["merge_commit_sha"]}}
    if result["state"] == "CLOSED":
        return result
    branch = quote(result["baseRefName"], safe="")
    try:
        protection = api(f"repos/{repo}/branches/{branch}/protection/required_status_checks",
                         profile_home=profile_home)
    except UnprotectedBranch:
        protection = {"contexts": [], "checks": []}
    # Both keys are part of the REST contract; missing policy is not no policy.
    required = [{"context": row["context"], "app": {"databaseId": row["app_id"]}}
                for row in protection["checks"]]
    pinned = {row["context"] for row in required}
    required.extend({"context": context, "app": None}
                    for context in protection["contexts"] if context not in pinned)
    result["baseRef"] = {"branchProtectionRule": {"requiredStatusChecks": required}}
    return result
