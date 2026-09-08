"""The FastAPI application — the Python counterpart to server/server.js.

Binds 127.0.0.1 only, exactly like the original: nothing about Jarvis is
reachable from anywhere else on the network, and that is a property of the app,
not of the user's firewall.

ONE PROCESS, ONE PORT. The Next.js front end is built to static files and served
by this app, rather than run as a second server on a second port. That keeps the
owner's launch experience to a single window and a single desktop shortcut,
which is the reason the original stack was chosen and is not worth giving up for
server-side rendering a local single-user app would never use.

Routes are registered as small routers under jarvis/routes/, grouped the same way
server.js groups them, so the port can be checked group by group against the
recorded contract fixtures rather than all at once.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .routes import (
    approvals, artifacts, connectors, control, conversations, core, events,
    models, notifications, skills, tasks, turn, uploads, voice,
)

# The built Next.js export. Absent during early migration, when the front end is
# still being served by the Node app — the API is fully usable without it.
FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "out"


def create_app() -> FastAPI:
    app = FastAPI(
        title="Jarvis",
        version="0.1.0",
        # The interactive docs are a development convenience only; they are not
        # part of the contract the front end depends on.
        docs_url="/api/_docs",
        openapi_url="/api/_openapi.json",
    )

    app.include_router(core.router)
    app.include_router(conversations.router)
    app.include_router(turn.router)
    app.include_router(events.router)
    app.include_router(approvals.router)
    app.include_router(voice.router)
    app.include_router(artifacts.router)
    app.include_router(uploads.router)
    app.include_router(skills.router)
    app.include_router(connectors.router)
    app.include_router(control.router)
    app.include_router(models.router)
    app.include_router(notifications.router)
    app.include_router(tasks.router)

    # Everything here is behind its own interlock and does nothing until
    # cutover — see assembly.start_background_work().
    from .assembly import start_background_work
    start_background_work()

    if FRONTEND_DIR.is_dir():
        # Mounted last so it can never shadow an /api route.
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

    return app


app = create_app()


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "3000"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
