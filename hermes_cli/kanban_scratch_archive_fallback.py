"""Optional Git-shaped archive preparation; unreadable repositories remain plain bytes."""
from __future__ import annotations

import json
import shutil


def prepare(workspace, partial, payload, layout, *, task_id, completed_at):
    """Return omission plans/records, or reset to a lossless filesystem-only archive.

    A Git inspection failure says nothing about whether files can be preserved.
    Throw away ALL optional output/plans before falling back: an earlier healthy
    repo may have produced an omission plan, or the failing one a partial diff.
    Do not catch payload, discovery, durability or archive-verification failures.
    External gitdirs are not self-contained; retain their existing refusal path.
    """
    from hermes_cli import kanban_scratch_archive as sa

    try:
        plans, records = sa._omission_plans(workspace, layout.repos, payload)
        shaped = ([(r, {"omitted": records.get(r)}) for r in layout.repos]
                  + [(r, {"bare": True}) for r in layout.bare]
                  + [(w, {"gitdir": g}) for w, g in sorted(layout.linked.items())])
        for repo, kwargs in shaped:
            rel = repo.relative_to(workspace)
            sa.archive_repo(repo, partial / rel if rel.parts else partial,
                            task_id=task_id, completed_at=completed_at, **kwargs)
        return plans, records
    except sa.GitInspectionFailed as exc:
        if any(not inside for _, inside in layout.linked.values()):
            raise sa.ArchiveRefused(f"external_gitdir: filesystem fallback not self-contained: {exc}") from exc
        # Only the unpublished temporary archive is removed, never source bytes.
        shutil.rmtree(partial)
        payload.mkdir(parents=True)
        (payload / "git-fallback.json").write_text(json.dumps({
            "mode": "complete-filesystem", "reason": str(exc),
            "note": "All workspace bytes retained without Git omissions. This preserves existing "
                    "data, not objects already absent from the source; Git repair may still be needed.",
        }, indent=2) + "\n", encoding="utf-8")
        sa.log.warning("Scratch archive filesystem fallback: task=%s path=%s; %s", task_id, workspace, exc)
        return [], {}
