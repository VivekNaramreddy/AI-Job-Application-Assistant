"""
run.py
=============================================================================
One-Click Launcher Script for LinkedIn Autopilot Job Applier

PYTHON LESSON:
- `webbrowser` is a built-in Python module to open web pages in the user's default browser.
- `uvicorn.run()` programmatically starts our FastAPI web server on host 127.0.0.1:8000.
=============================================================================
"""

import sys
import time
import webbrowser
from pathlib import Path

# On Windows, force ProactorEventLoop policy so Playwright can launch subprocesses
if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import uvicorn

BASE_DIR = Path(__file__).resolve().parent

def main():
    print("=" * 70)
    print("🚀 LinkedIn Autopilot & Job Applier Launcher")
    print("=" * 70)
    print("📍 Dashboard URL : http://127.0.0.1:8000")
    print("📍 Documentation : http://127.0.0.1:8000/docs")
    print("=" * 70)

    # Open browser automatically after a short delay
    def open_browser():
        time.sleep(1.5)
        print("🌐 Opening Web Dashboard in browser...")
        webbrowser.open("http://127.0.0.1:8000")

    import threading
    threading.Thread(target=open_browser, daemon=True).start()

    # IMPORTANT ON WINDOWS:
    # Uvicorn's reload mode starts a child process and selects an event loop that
    # cannot launch Playwright's browser-driver subprocess. This application is
    # a normal desktop-style launcher, not a development server, so automatic
    # source-code reloading is unnecessary. With reload disabled, Uvicorn uses
    # Windows' ProactorEventLoop and Playwright can start correctly.
    uvicorn.run(
        "src.main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        loop="asyncio",
    )

if __name__ == "__main__":
    main()
