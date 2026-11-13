"""Channel/thread skill bindings for platform adapters."""


def resolve_channel_skills(
    config_extra: dict, channel_id: str, parent_id: str | None = None) -> list[str] | None:
    """Auto-loaded skill(s) for a channel/thread from ``channel_skill_bindings`` (entries
    ``{id: "<channel/forum id>", skills: [...]}``; ``skill: "<name>"`` also accepted). Exact
    *channel_id* first, then *parent_id* (threads inherit). Deduplicated ordered list or None."""
    bindings = config_extra.get("channel_skill_bindings") or []
    if not isinstance(bindings, list) or not bindings:
        return None
    # One pass per id, not one pass matching either: the parent's entry may be listed first.
    for wanted in (str(key) for key in (channel_id, parent_id) if key):
        for entry in bindings:
            if not isinstance(entry, dict) or str(entry.get("id", "")) != wanted:
                continue
            skills = entry.get("skills") or entry.get("skill")
            if isinstance(skills, str):
                return [skills.strip()] if skills.strip() else None
            if isinstance(skills, list) and skills:
                seen = dict.fromkeys(
                    nm for name in skills if isinstance(name, str) and (nm := name.strip()))
                return list(seen) or None
    return None
