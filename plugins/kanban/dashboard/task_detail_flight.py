"""In-flight-only joining for overlapping task-drawer history reads. [daveey]"""
from typing import Optional

from fastapi import Query

def mount(router, get_task, coalesced_read, current_board):
    # Four tracked flights, not a new threadpool cap. Excess distinct resources
    # keep their original runner admission; identical tracked polls share work.
    read = coalesced_read(get_task, admission_key=lambda a: tuple(a.values()),
                          max_concurrent=4, overflow_to_runner=True)

    @router.get("/tasks/{task_id}")
    async def get_task_endpoint(
        task_id: str, board: Optional[str] = Query(None),
        run_state_type: Optional[str] = Query(None, description="With run_state_name: filter runs by column 'status' or 'outcome'"),
        run_state_name: Optional[str] = Query(None, description="With run_state_type: exact value for that run column")):
        # Select before keying: a board switch must never join the old board.
        return await read(task_id=task_id, board=board or current_board(),
                          run_state_type=run_state_type, run_state_name=run_state_name)
