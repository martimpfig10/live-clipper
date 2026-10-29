"""ffmpeg / ffprobe helpers."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np


class MediaError(RuntimeError):
    pass


def run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace")[-1500:]
        raise MediaError(f"{cmd[0]} failed ({proc.returncode}): {tail}")
    return proc


def probe(path: str | Path) -> dict:
    """Return {duration, width, height, has_audio}."""
    out = run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    ).stdout
    info = json.loads(out)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if v is None:
        raise MediaError(f"No video stream in {path}")
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = abs(int(sd["rotation"]))
    if rot in (90, 270):
        w, h = h, w
    duration = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
    return {"duration": duration, "width": w, "height": h, "has_audio": a is not None}


def extract_audio_chunks(
    src: str | Path, out_dir: str | Path, duration: float, chunk_seconds: int = 600, overlap: float = 2.0
) -> list[tuple[float, Path]]:
    """Cut the source into mono 16 kHz mp3 chunks (small enough for Whisper APIs).

    Returns [(offset_seconds, path)]. Each chunk is `chunk_seconds + overlap` long so words
    on the boundary are not lost; transcribe.merge() drops the duplicated overlap.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks = []
    offset = 0.0
    i = 0
    while offset < duration:
        p = out_dir / f"audio_{i:03d}.mp3"
        if not p.exists():
            run([
                "ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{offset:.3f}", "-i", str(src),
                "-t", f"{chunk_seconds + overlap:.3f}", "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "libmp3lame", "-b:a", "48k", str(p),
            ])
        chunks.append((offset, p))
        offset += chunk_seconds
        i += 1
    return chunks


def loudness_per_second(src: str | Path, sample_rate: int = 8000) -> np.ndarray:
    """RMS loudness in dBFS for every second of audio. Cheap: decodes at 8 kHz mono."""
    proc = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(src), "-vn", "-ac", "1",
         "-ar", str(sample_rate), "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    block = sample_rate * 2  # bytes per second
    values = []
    assert proc.stdout is not None
    while True:
        buf = proc.stdout.read(block)
        if not buf:
            break
        x = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
        values.append(20 * np.log10(rms + 1e-6))
    proc.wait()
    return np.array(values, dtype=np.float32)


def extract_frame(src: str | Path, t: float, out: str | Path, width: int = 720) -> Path:
    run([
        "ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(src),
        "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3", str(out),
    ])
    return Path(out)
