"""Local launch of OpsCenter: uvicorn on 127.0.0.1 + opening the browser.

The application is single-user and listens only on localhost — no external
addresses.
"""

from __future__ import annotations

import threading
import webbrowser

import uvicorn
from dotenv import load_dotenv

# Load .env before starting the server and building any Gemini client.
load_dotenv()

from app.config import HOST, PORT  # noqa: E402  (after load_dotenv by design)


def main() -> None:
    # Open the browser a little later, giving the server time to come up.
    threading.Timer(1.0, lambda: webbrowser.open(f"http://{HOST}:{PORT}/")).start()
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()
