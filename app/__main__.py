"""Start the site:  python -m app   (phone: type tv · Windows: the desktop icon)"""
import os
import shutil
import socket
import subprocess
import threading
import webbrowser

import uvicorn

from .main import create_app


def lan_address() -> str | None:
    """This device's address on the home Wi-Fi (for opening the site from another device)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; picks the outgoing interface
            ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000") or 8000)
    # HOST=0.0.0.0: other devices on the Wi-Fi may open the site (the computer).
    # Not set (the phone): the site still listens on the Wi-Fi, but ONLY a TV that was handed a video
    # (Chromecast, with a one-time key) gets an answer; everything else from outside is refused.
    host = os.getenv("HOST") or "0.0.0.0"
    if not os.getenv("HOST"):
        os.environ["LOCAL_ONLY"] = "1"
    url = f"http://localhost:{port}"
    print(f"\n  *** The site is running! Open in the browser: {url}")
    if os.getenv("HOST") == "0.0.0.0" and lan_address():
        print(f"  From your phone (same Wi-Fi): http://{lan_address()}:{port}")
    print("  To stop: CTRL+C (or close this window)\n")
    if os.getenv("NO_BROWSER") != "1":
        if shutil.which("termux-open-url"):
            threading.Timer(2.5, lambda: subprocess.run(["termux-open-url", url], check=False)).start()
        elif os.name == "nt":
            threading.Timer(2.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
