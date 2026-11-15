"""Stable post-merge sampling, independent of board replicas and intake policy.

The caller verifies the repository and landed SHA and supplies complete changes.
The fixed seed makes retries and independent sweep producers choose identically.
"""
from __future__ import annotations

import hashlib
import math
import re

DEFAULT_SAMPLE_RATE = 0.2
SAMPLE_SEED = "hermes-post-merge-v1"


def sample_rate(config):
    """One reversible setting: kanban.self_work.sample_rate (1 = all fixes)."""
    section = config
    for key in ("kanban", "self_work"):
        if not isinstance(section, dict):
            raise ValueError("sampling configuration sections must be objects")
        section = section.get(key, {})
    if not isinstance(section, dict):
        raise ValueError("sampling configuration sections must be objects")
    rate = section.get("sample_rate", DEFAULT_SAMPLE_RATE)
    if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 <= rate <= 1:
        raise ValueError("kanban.self_work.sample_rate must be between 0 and 1")
    return float(rate)


def decide(*, repository, merged_sha, forced, config, seed=SAMPLE_SEED):
    """Return a reproducible draw; no writes, random rerolls or host identity.

    ``forced`` comes from the trusted complete-diff classifier, never a title.
    Unknown classification is selected rather than silently sampled away.
    ``seed`` is injectable for deterministic tests, not an extra config knob.
    """
    if not isinstance(repository, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError("sampling requires owner/repository")
    if not isinstance(merged_sha, str) or not re.fullmatch("[0-9a-f]{40}", merged_sha):
        raise ValueError("sampling requires the verified full merged SHA")
    if not isinstance(seed, str) or not seed or "\0" in seed:
        raise ValueError("sampling seed must be a nonempty string without NUL")
    rate = sample_rate(config)
    identity = "\0".join((seed, repository.lower(), merged_sha))
    draw = int.from_bytes(hashlib.sha256(identity.encode("utf-8")).digest()[:8], "big") >> 11
    draw /= 1 << 53
    return {"merged_sha": merged_sha, "repository": repository.lower(),
            "draw": draw, "forced": forced is not False,
            "selected": forced is not False or draw < rate,
            "sample_rate": rate, "policy_version": SAMPLE_SEED}
