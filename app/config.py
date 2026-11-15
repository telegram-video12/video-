"""Settings: environment / .env first, then data/config.json (edited from the website)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip() or default)
    except (TypeError, ValueError):
        return default


class Settings:
    EDITABLE = ("api_id", "api_hash", "tmdb_api_key", "password_hash")

    def __init__(self) -> None:
        self.data_dir = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "thumbs").mkdir(exist_ok=True)

        self.api_id = _int(os.getenv("TG_API_ID"))
        self.api_hash = os.getenv("TG_API_HASH", "").strip()
        self.tmdb_api_key = os.getenv("TMDB_API_KEY", "").strip()
        self.env_password = os.getenv("SITE_PASSWORD", "").strip()
        self.password_hash = ""
        self.scan_interval_minutes = _int(os.getenv("SCAN_INTERVAL_MINUTES"), 30)
        self.min_duration_seconds = _int(os.getenv("MIN_DURATION_SECONDS"), 180)

        for key, value in self._load().items():
            if key in self.EDITABLE and value not in (None, ""):
                setattr(self, key, _int(value) if key == "api_id" else value)

    # ------------------------------------------------------------ persistence
    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.json"

    def _load(self) -> dict:
        try:
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, **values) -> None:
        data = self._load()
        for key, value in values.items():
            if key not in self.EDITABLE:
                continue
            value = _int(value) if key == "api_id" else str(value or "").strip()
            data[key] = value
            setattr(self, key, value)
        self.config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------------------------------------------------------- paths
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

    # ------------------------------------------------------------- password
    @property
    def needs_setup(self) -> bool:
        return not (self.password_hash or self.env_password)

    def set_password(self, password: str) -> None:
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
        self.save(password_hash=f"{salt}${digest}")

    def check_password(self, password: str) -> bool:
        if self.password_hash and "$" in self.password_hash:
            salt, digest = self.password_hash.split("$", 1)
            got = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
            return hmac.compare_digest(got, digest)
        if self.env_password:
            return hmac.compare_digest(password, self.env_password)
        return False
