"""Settings loaded from environment / .env file."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


@dataclass
class Settings:
    api_id: int = field(default_factory=lambda: _int("TG_API_ID", 0))
    api_hash: str = field(default_factory=lambda: os.getenv("TG_API_HASH", "").strip())
    site_password: str = field(default_factory=lambda: os.getenv("SITE_PASSWORD", "").strip())
    tmdb_api_key: str = field(default_factory=lambda: os.getenv("TMDB_API_KEY", "").strip())
    scan_interval_minutes: int = field(default_factory=lambda: _int("SCAN_INTERVAL_MINUTES", 30))
    min_duration_seconds: int = field(default_factory=lambda: _int("MIN_DURATION_SECONDS", 180))
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", str(ROOT / "data"))))

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "thumbs").mkdir(exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "library.db"

    @property
    def session_path(self) -> Path:
        return self.data_dir / "telegram"  # Telethon appends ".session"

    @property
    def secret_key(self) -> str:
        path = self.data_dir / "secret.key"
        if not path.exists():
            path.write_text(secrets.token_hex(32))
        return path.read_text().strip()
