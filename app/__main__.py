"""Start the site:  python -m app"""
import os

import uvicorn

from .main import create_app

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000") or 8000)
    print(f"\n  האתר עולה בכתובת:  http://localhost:{port}\n")
    uvicorn.run(create_app(), host=os.getenv("HOST", "0.0.0.0"), port=port, log_level="info")
