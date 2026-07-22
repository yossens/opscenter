"""OpsCenter HTML pages (Jinja2). Data is loaded by JS via /api.

In Step 1 (T1) these are route stubs; the full UI scaffold arrives with T9.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()


def _render(request: Request, template: str, title: str) -> HTMLResponse:
    """Renders a Jinja2 template with context (the page title)."""
    return templates.TemplateResponse(request, template, {"title": title})


@router.get("/", response_class=HTMLResponse)
def inbox_page(request: Request) -> HTMLResponse:
    return _render(request, "inbox.html", "Inbox")


@router.get("/board", response_class=HTMLResponse)
def board_page(request: Request) -> HTMLResponse:
    return _render(request, "board.html", "Board")


@router.get("/deals/{deal_id}", response_class=HTMLResponse)
def deal_page(request: Request, deal_id: int) -> HTMLResponse:
    return _render(request, "deal.html", "Item")


@router.get("/archive", response_class=HTMLResponse)
def archive_page(request: Request) -> HTMLResponse:
    return _render(request, "archive.html", "Archive")


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    return _render(request, "settings.html", "Settings")
