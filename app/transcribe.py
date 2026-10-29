"""Speech-to-text through any OpenAI-compatible Whisper endpoint (default: Groq)."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .config import Settings
from .media import extract_audio_chunks

log = logging.getLogger(__name__)


def _client(s: Settings):
    from openai import OpenAI

    return OpenAI(base_url=s.stt_base_url, api_key=s.stt_api_key or "missing", max_retries=0)


def _transcribe_file(s: Settings, path: Path) -> dict:
    cfg = s.section("transcription")
    client = _client(s)
    delay = 5.0
    for attempt in range(8):
        try:
            with open(path, "rb") as f:
                resp = client.audio.transcriptions.create(
                    model=s.stt_model,
                    file=f,
                    language=cfg.get("language", "pt"),
                    prompt=cfg.get("prompt") or None,
                    response_format="verbose_json",
                    timestamp_granularities=["word", "segment"],
                    temperature=0.0,
                )
            return resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
        except Exception as e:  # rate limits / transient errors
            status = getattr(e, "status_code", None)
            if status in (400, 401, 403, 404):
                raise
            log.warning("transcription attempt %d failed (%s); retrying in %.0fs", attempt + 1, e, delay)
            time.sleep(delay)
            delay = min(delay * 2, 120)
    raise RuntimeError(f"Transcription failed for {path}")


def merge(parts: list[tuple[float, float, dict]]) -> dict:
    """Merge chunk transcripts into one, shifting timestamps and dropping overlaps.

    parts: [(offset, keep_until, response)] — words whose absolute start is >= keep_until
    belong to the next chunk.
    """
    words, segments = [], []
    for offset, keep_until, resp in parts:
        for w in resp.get("words") or []:
            st = float(w["start"]) + offset
            if st < offset - 0.01 or st >= keep_until:
                continue
            words.append({"word": str(w["word"]).strip(), "start": st, "end": float(w["end"]) + offset})
        for seg in resp.get("segments") or []:
            st = float(seg["start"]) + offset
            if st >= keep_until:
                continue
            segments.append({
                "start": st,
                "end": float(seg["end"]) + offset,
                "text": str(seg.get("text", "")).strip(),
            })
    words.sort(key=lambda w: w["start"])
    segments.sort(key=lambda x: x["start"])
    return {"words": words, "segments": segments}


def transcribe_source(s: Settings, src: Path, duration: float, work_dir: Path) -> dict:
    out = work_dir / "transcript.json"
    if out.exists():
        return json.loads(out.read_text())
    chunk_s = int(s.section("transcription").get("chunk_seconds", 600))
    chunks = extract_audio_chunks(src, work_dir / "audio", duration, chunk_s)
    parts = []
    for i, (offset, path) in enumerate(chunks):
        cache = path.with_suffix(".json")
        if cache.exists():
            resp = json.loads(cache.read_text())
        else:
            log.info("transcribing chunk %d/%d", i + 1, len(chunks))
            resp = _transcribe_file(s, path)
            cache.write_text(json.dumps(resp, ensure_ascii=False))
        parts.append((offset, offset + chunk_s, resp))
    transcript = merge(parts)
    out.write_text(json.dumps(transcript, ensure_ascii=False))
    return transcript
