"""Skills-index row labelling and rendering for ``agent.prompt_builder`` (moved out of the facade). [daveey]"""


def _label_visible_entries(visible_entries: list[dict], skills_by_category: dict[str, list[tuple[str, str]]],
                           with_triggers: bool = True) -> None:
    """Index rows under each entry's ``load_name`` (what skill_view accepts); ``with_triggers=False`` drops
    the ``triggers:`` suffix a row's description carries."""
    from agent.skill_utils import TIER_PROJECT
    for entry in (e for e in visible_entries if e["load_name"]):
        desc = entry.get("description", "")
        triggers = entry.get("triggers") or ""
        if not with_triggers and triggers and desc.endswith(triggers):
            desc = desc[:-len(triggers)]
        if entry["tier"] == TIER_PROJECT:
            desc = f"[project] {desc}".strip()
        category = entry.get("category") or "general"
        skills_by_category.setdefault(category, []).append((entry["load_name"], desc))


def _render_skills_index(
    skills_by_category: dict[str, list[tuple[str, str]]],
    compact_categories: frozenset[str] | None, available_tools: set[str] | None, unloadable: list[str] = (),
    index_mode: str = "full",
) -> str:
    """Render the ## Skills block; "" when there is nothing to list. *unloadable* names (different skills
    sharing a name AND relative path within one tier — one root or several) get a rename note instead of a row skill_view would refuse.
    ``index_mode="names"`` demotes every category (a one-skill category is a bare ``- name`` row)."""
    if not skills_by_category:
        return ""
    names_only = index_mode == "names"
    # Demoted categories collapse to one names-only line. NEVER drop entries — agent-created skills are the
    # model's project memory and it won't rediscover them via skills_list. Nested categories follow their parent.
    demoted = frozenset(skills_by_category) if names_only else frozenset(
        cat for cat in skills_by_category if cat.split("/", 1)[0] in (compact_categories or frozenset()))
    hidden_note = (
        "\n(This Kanban worker's index lists skill names only, to keep per-call context small — every skill "
        "loads with skill_view(name) as usual, and skills_list shows descriptions.)"
    ) if names_only else (
        "\n(Categories marked [names only] are outside the current coding "
        "context, so their descriptions are omitted — the skills work "
        "normally and load with skill_view(name) as usual.)"
    ) if demoted else ""
    if unloadable:
        hidden_note += (f"\n(A copy of {', '.join(unloadable)} is not listed: it shares both its name and its path "
                        "with a different skill in the same skills directory tier (e.g. another external_dirs entry), "
                        "so skill_view cannot load it — rename one.)")
    # Don't name web_search when the session has no web tools (dangling reference).
    _basic_tools = "terminal" if available_tools is not None and "web_search" not in available_tools else "web_search or terminal"
    index_lines = []
    for category in sorted(skills_by_category):
        entries = skills_by_category[category]
        if category in demoted:
            names = sorted({n for n, _ in entries})
            if names_only and names == [category]:
                index_lines.append(f"  - {category}")
                continue
            index_lines.append(f"  {category}{'' if names_only else ' [names only]'}: {', '.join(names)}")
            continue
        rows: dict[str, str] = {}
        for name, desc in sorted(entries, key=lambda x: x[0]):  # stable: first entry per name wins
            rows.setdefault(name, desc)
        # A one-skill category is a single top-level row: its header would only repeat what the row
        # says (a skill at the top of the tree is its own category, so the name printed twice).
        indent = "  " if len(rows) == 1 else "    "
        if len(rows) > 1:
            index_lines.append(f"  {category}:")
        index_lines.extend(f"{indent}- {name}: {desc}" if desc else f"{indent}- {name}" for name, desc in rows.items())
    from agent.oneshot_footprint import ONESHOT_SKILLS_LOAD_GUIDANCE, is_single_query_session
    if is_single_query_session():
        return (
            ONESHOT_SKILLS_LOAD_GUIDANCE
            + "\n<available_skills>\n" + "\n".join(index_lines) + "\n</available_skills>"
            + hidden_note
        )
    return (
        "## Skills\n"
        "Before replying, scan the skills below. If a skill matches or is even partially relevant to your "
        "task, you MUST load it with skill_view(name) and follow its instructions. Err on the side of "
        "loading — it is always better to have context you don't need than to miss critical steps, pitfalls, "
        "or established workflows. Skills contain specialized knowledge — API endpoints, tool-specific "
        "commands, and proven workflows that outperform general-purpose approaches. Load the skill "
        f"even if you think you could handle the task with basic tools like {_basic_tools}. "
        "Skills also encode the user's preferred approach, conventions, and quality standards for tasks like "
        "code review, planning, and testing — load them even for tasks you already know how to do, because "
        "the skill defines how it should be done here.\n"
        "If a skill has issues, fix it with skill_manage(action='patch').\n"
        "After difficult/iterative tasks, offer to save as a skill. If a skill you loaded was missing steps, "
        "had wrong commands, or needed pitfalls you discovered, update it before finishing.\n"
        "\n"
        "<available_skills>\n"
        + "\n".join(index_lines) + "\n"
        "</available_skills>\n\n"
        "Only proceed without loading a skill if genuinely none are relevant to the task."
        + hidden_note
    )
