"""T8 tests: the `GET /dashboard` page + the nav link in `base.html`.

Acceptance criteria come from docs/specs/006-custom-improvements.md, task T8
(lines 281-290). The spec marks only two criteria as automatable:

1. `GET /dashboard` returns 200 `text/html` and the body contains `<html`.
2. The `base.html` header contains a link with `href="/dashboard"`.

Everything about the actual data rendering in `dashboard.js`
(`/api/stats` -> cards/bars/summary) is explicitly marked by the spec as
manual-verification-only and is not tested here.

At the time the test was written, the implementation
(`app/routers/dashboard.py` — the `GET /dashboard` route,
`app/templates/dashboard.html`, `app/templates/base.html` — the nav link) was
NOT read and `/dashboard` did not exist — the correct TDD state: the file
collects (`--collect-only` green) and the tests fail until implementation (404
on `/dashboard`; or the missing link in `base.html` until T8 lands).

Only the `tests/conftest.py::client` fixture is used — the same `TestClient`
already used to check existing page routes
(see `tests/test_pings_block.py::test_pings_zero_stages_zero_deals_index_and_board_pages_still_200`,
where pages are checked with `assert client.get(...).status_code == 200`,
without parsing HTML). To check the nav we reuse the existing render of
`base.html` via `GET /` (the home page, `app/routers/pages.py`), without
touching `app/routers/dashboard.py`.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# T8 criterion 1: GET /dashboard -> 200 text/html, body contains <html
# ---------------------------------------------------------------------------


def test_get_dashboard_returns_200_text_html(client):
    response = client.get("/dashboard")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")


def test_get_dashboard_body_contains_html_tag(client):
    response = client.get("/dashboard")
    assert response.status_code == 200, response.text
    assert "<html" in response.text.lower()


# ---------------------------------------------------------------------------
# T8 criterion 2: the header nav in base.html contains an href="/dashboard" link
#
# base.html is the shared layout rendered by any page (not only
# dashboard.html). We check it via the existing "/" route so this part of the
# criterion does not depend on the T8 route's own implementation.
# ---------------------------------------------------------------------------


def test_base_html_header_nav_contains_dashboard_link(client):
    response = client.get("/")
    assert response.status_code == 200, response.text
    assert 'href="/dashboard"' in response.text
