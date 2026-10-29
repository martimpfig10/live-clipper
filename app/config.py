"""Settings: secrets from env, behaviour from config.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSans-Black.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-ExtraBold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


@dataclass
class Account:
    name: str
    slots: list[str]


@dataclass
class Settings:
    raw: dict
    data_dir: Path
    groq_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    stt_base_url: str = ""
    stt_model: str = ""
    stt_api_key: str = ""
    app_user: str = "admin"
    app_password: str = ""
    accounts: list[Account] = field(default_factory=list)

    # --- convenience accessors -------------------------------------------------
    def section(self, name: str) -> dict:
        return self.raw.get(name, {}) or {}

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.raw.get("timezone", "Europe/Lisbon"))

    @property
    def creator(self) -> str:
        return self.section("creator").get("username", "")

    @property
    def credit_line(self) -> str:
        return self.section("creator").get("credit_line", "")

    @property
    def font_path(self) -> str:
        configured = self.section("render").get("font_path") or ""
        if configured and Path(configured).exists():
            return configured
        for cand in FONT_CANDIDATES:
            if Path(cand).exists():
                return cand
        return ""  # Pillow default font as last resort

    # --- directories -----------------------------------------------------------
    @property
    def inbox(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def recordings(self) -> Path:
        return self.data_dir / "recordings"

    @property
    def work(self) -> Path:
        return self.data_dir / "work"

    @property
    def photos(self) -> Path:
        return self.data_dir / "photos"

    @property
    def renders(self) -> Path:
        return self.data_dir / "renders"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clipper.db"

    def ensure_dirs(self) -> None:
        for d in (self.inbox, self.recordings, self.work, self.photos, self.renders):
            d.mkdir(parents=True, exist_ok=True)


def load_settings(config_path: str | os.PathLike | None = None) -> Settings:
    path = Path(config_path or os.environ.get("CONFIG_PATH", ROOT / "config.yaml"))
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    groq = os.environ.get("GROQ_API_KEY", "")
    s = Settings(
        raw=raw,
        data_dir=Path(os.environ.get("DATA_DIR", ROOT / "data")),
        groq_api_key=groq,
        llm_base_url=os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1"),
        llm_model=os.environ.get("LLM_MODEL", "openai/gpt-oss-120b"),
        llm_api_key=os.environ.get("LLM_API_KEY") or groq,
        stt_base_url=os.environ.get("STT_BASE_URL", "https://api.groq.com/openai/v1"),
        stt_model=os.environ.get("STT_MODEL", "whisper-large-v3"),
        stt_api_key=os.environ.get("STT_API_KEY") or groq,
        app_user=os.environ.get("APP_USER", "admin"),
        app_password=os.environ.get("APP_PASSWORD", ""),
        accounts=[Account(a["name"], list(a.get("slots", []))) for a in raw.get("accounts", [])],
    )
    s.ensure_dirs()
    return s


@lru_cache
def get_settings() -> Settings:
    return load_settings()
