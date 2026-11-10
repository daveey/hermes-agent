"""Map exhausted board lifecycle reads to retryable HTTP, including plugin routes. [daveey]"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from hermes_cli.kanban_generation import BoardLifecycleBusy


def install_board_busy_handler(app: FastAPI) -> None:
    # Keep the storage layer HTTP-independent. Its bounded retries and stale memo
    # still run first; only the typed lifecycle refusal reaches this boundary.
    # In-process plugin routers (user-steps/session-cards) share this application.
    async def board_busy(_request: Request, _exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": "Kanban board is busy; retry shortly."},
            headers={"Retry-After": "1"},
        )

    app.add_exception_handler(BoardLifecycleBusy, board_busy)
