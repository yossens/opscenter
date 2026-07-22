"""Assembly of the OpsCenter FastAPI application."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Load .env before building any Gemini client (the SDK reads GEMINI_API_KEY from env).
load_dotenv()

from . import db
from .routers import (
    attachments,
    dashboard,
    deals,
    llm,
    notes,
    pages,
    parsing,
    pings,
    search,
    settings,
    stages,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Application lifecycle: DB initialization."""
    db.init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="OpsCenter", lifespan=_lifespan)

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Browsers request /favicon.ico on their own — serve the same SVG to avoid a 404.
    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    app.include_router(pages.router)
    app.include_router(notes.router)
    app.include_router(attachments.router)
    app.include_router(deals.router)
    app.include_router(stages.router)
    app.include_router(search.router)
    app.include_router(pings.router)
    app.include_router(settings.router)
    app.include_router(parsing.router)
    app.include_router(llm.router)
    app.include_router(dashboard.router)
    app.include_router(dashboard.page_router)

    return app


app = create_app()
