"""Entry point.

Starts the server on localhost and opens it in a window.

The browser path has to be resolved before anything imports Playwright,
which is why it happens at the top of this file rather than inside the app
package. A colleague's machine will not have run `playwright install`, so
the browser ships in a folder beside the executable and is pointed at from
here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def base_dir() -> Path:
    """The folder the app was launched from, frozen or not."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def locate_browser() -> str | None:
    """Point Playwright at the browser shipped alongside the executable.

    Without this, Playwright looks in the user's AppData folder, which is
    only populated if that person has run `playwright install` themselves.
    Setting the variable before import is what lets the app work on a
    machine with nothing installed on it.
    """
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return os.environ["PLAYWRIGHT_BROWSERS_PATH"]

    candidates = (
        base_dir() / "ms-playwright",
        base_dir() / "_internal" / "ms-playwright",
    )
    for candidate in candidates:
        if candidate.is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(candidate)
            return str(candidate)
    return None


BROWSER_PATH = locate_browser()

import socket        # noqa: E402
import threading     # noqa: E402
import time          # noqa: E402
import webbrowser    # noqa: E402

import uvicorn       # noqa: E402

from app.main import app  # noqa: E402

HOST = "127.0.0.1"


def free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return int(s.getsockname()[1])


def serve(port: int) -> None:
    uvicorn.run(app, host=HOST, port=port, log_level="warning")


def main() -> None:
    if BROWSER_PATH is None:
        print("Note: no bundled browser folder found. Falling back to the "
              "Playwright cache in AppData.")

    port = free_port()
    url = f"http://{HOST}:{port}"

    thread = threading.Thread(target=serve, args=(port,), daemon=True)
    thread.start()
    time.sleep(1.2)  # Let uvicorn bind before the window asks for the page.

    try:
        import webview  # pywebview
        # Off by default, which would make "Download settings JSON" a
        # button that silently does nothing.
        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.create_window("Campkins Batch Uploader", url,
                              width=1360, height=940, min_size=(960, 640))
        webview.start()
    except ImportError:
        webbrowser.open(url)
        print(f"Running at {url}\nClose this window to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
