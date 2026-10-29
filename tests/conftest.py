import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    from app.config import load_settings

    return load_settings(ROOT / "config.yaml")


@pytest.fixture
def db(settings):
    from app.db import DB

    return DB(settings.db_path)


def make_video(path: Path, seconds: int = 20, w: int = 720, h: int = 1280) -> Path:
    """Synthetic vertical 'live': test pattern + a tone that gets loud every 30s."""
    subprocess.run([
        "ffmpeg", "-nostdin", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-af", "volume='if(lt(mod(t,30),5),1.0,0.1)':eval=frame",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(path),
    ], check=True)
    return path


@pytest.fixture
def video(tmp_path):
    return make_video(tmp_path / "live.mp4")
