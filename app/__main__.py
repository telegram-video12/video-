"""Start the site:  python -m app   (on the phone, just type: tv)"""
import os
import shutil
import subprocess
import threading

import uvicorn

from .main import create_app

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000") or 8000)
    # 127.0.0.1 = reachable only from this phone. Set HOST=0.0.0.0 to allow other devices on the Wi-Fi.
    host = os.getenv("HOST", "127.0.0.1")
    url = f"http://localhost:{port}"
    print(f"\n  האתר פועל!  פתח בדפדפן:  {url}\n  כדי לעצור: CTRL+C (או לסגור את Termux)\n")
    if shutil.which("termux-open-url") and os.getenv("NO_BROWSER") != "1":
        threading.Timer(2.5, lambda: subprocess.run(["termux-open-url", url], check=False)).start()
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
