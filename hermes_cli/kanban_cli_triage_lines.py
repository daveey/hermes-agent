"""Result-line formatters for `kanban specify` / `kanban decompose` (moved out of the facade)."""


def _retitled_suffix(outcome) -> str:
    return f" — retitled: {outcome.new_title!r}" if outcome.new_title else ""


def _decompose_ok_line(o) -> str:
    if o.fanout and o.child_ids:
        return (f"Decomposed {o.task_id} → {len(o.child_ids)} "
                f"children ({', '.join(o.child_ids)}); root promoted to todo")
    return f"Specified {o.task_id} → todo (no fanout){_retitled_suffix(o)}"
