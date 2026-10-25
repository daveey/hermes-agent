"""Account-plan eligibility shared by Codex discovery and runtime selection."""
from typing import Any, Optional

# ChatGPT plan tiers that cannot run account-gated Codex models (Astra). Unknown/missing plan claims
# are NOT excluded: only a positively-identified free tier is skipped.
_CODEX_NON_ENTITLED_PLANS = frozenset({"free", "go", "guest"})


def codex_plan_type(access_token: Any) -> Optional[str]:
    """Lower-cased ``chatgpt_plan_type`` claim of a Codex access token, or None when absent."""
    from agent.credential_pool import _decode_jwt_claims

    claims = _decode_jwt_claims(access_token)
    auth_claims = claims.get("https://api.openai.com/auth") if isinstance(claims, dict) else None
    plan = auth_claims.get("chatgpt_plan_type") if isinstance(auth_claims, dict) else None
    return plan.strip().lower() if isinstance(plan, str) and plan.strip() else None


def codex_entry_entitled_for_model(access_token: Any, model: Optional[str]) -> bool:
    """False when *model* is account-gated (Astra) and the token's plan is a known non-entitled tier.
    A request for it must never rotate onto such an account: it would 4xx or, worse, silently reroute."""
    from agent.reasoning_effort import is_astra_model

    if not model or not is_astra_model(model):
        return True
    return codex_plan_type(access_token) not in _CODEX_NON_ENTITLED_PLANS


