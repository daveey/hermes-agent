"""Pure canonical board switcher/project catalog wire projections.

Inputs are captured canonical plain containers. Capture failures are handled by
source health, not converted to empty inputs here. No store or source access.
"""
from copy import deepcopy


def projects(rows):
    """Project.to_dict() rows in canonical list_projects order, already filtered."""
    return {"projects": [
        {"id": p["id"], "slug": p["slug"], "name": p["name"],
         "primary_path": p.get("primary_path") or "", "icon": p.get("icon") or "",
         "color": p.get("color") or ""} for p in rows]}


def boards(rows, current, *, counts, workspace_kinds, project_names):
    """Preserve metadata fields/order; require complete per-board captures.

    counts/workspace_kinds are slug keyed; project_names is project-id keyed
    and includes archived projects. Unknown project IDs retain legacy null names.
    """
    result = deepcopy(rows)
    for board in result:
        slug = board["slug"]
        board["is_current"] = slug == current
        board["counts"] = deepcopy(counts[slug])
        board["total"] = sum(n for status, n in board["counts"].items() if status != "archived")
        board["default_workspace_kind"] = workspace_kinds[slug]
        pid = board["project_id"] = board.get("project_id") or None
        board["project_name"] = project_names.get(pid) if pid else None
    return {"boards": result, "current": current}
