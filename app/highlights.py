"""Find clip-worthy moments in a transcript with an LLM + loudness signal."""
from __future__ import annotations

import logging
import re

import numpy as np

from .config import Settings
from .llm import chat_json

log = logging.getLogger(__name__)

SYSTEM = """És um editor de clips de TikTok experiente. Recebes a transcrição (com tempos em segundos) \
de uma live do TikTok do criador português "{creator}". Nas lives ele fala muitas vezes com convidados \
brasileiros, por isso há português de Portugal (pt-PT) e do Brasil (pt-BR) misturados.

Escolhe os melhores momentos para clips verticais curtos. Um bom clip:
- funciona sozinho, sem contexto (quem não viu a live percebe);
- prende nos primeiros 3 segundos (começa já na frase forte, não em "então...", "pronto...");
- acaba no remate (piada, resposta, reação), não a meio de uma frase;
- tem entre {min_s} e {max_s} segundos (ideal ~{target_s}s);
- é engraçado, polémico, emotivo, surpreendente, ou tem uma história/conselho com valor.

Evita: leitura de presentes/gifts e agradecimentos, "segue a página", silêncios, problemas técnicos, \
dados pessoais (moradas, telefones), e conteúdo que humilhe alguém.

Linhas marcadas com [ALTO] tiveram volume acima do normal (risos, gritos, reações) — são bons sinais \
mas não obrigatórios.

Responde APENAS com JSON neste formato:
{{"clips": [{{
  "start": <segundos, número>,
  "end": <segundos, número>,
  "hook": "<título curto em cima do vídeo, máx 8 palavras, sem emojis, no dialeto de quem fala>",
  "category": "<engraçado|polémica|história|conselho|reação|outro>",
  "main_speaker": "<host|guest>",
  "dialect": "<pt-PT|pt-BR>",
  "score": <1-10, potencial viral>,
  "reason": "<1 frase: porque é bom>"
}}]}}
Devolve no máximo {max_n} clips. Se não houver nada bom, devolve {{"clips": []}}."""


def _fmt(t: float) -> str:
    return f"{t:.1f}"


def build_windows(segments: list[dict], window_s: float, overlap_s: float = 60.0) -> list[tuple[float, float]]:
    if not segments:
        return []
    end = segments[-1]["end"]
    windows, start = [], 0.0
    while start < end:
        windows.append((start, min(start + window_s, end)))
        if start + window_s >= end:
            break
        start += window_s - overlap_s
    return windows


def render_window(segments: list[dict], loud: np.ndarray, a: float, b: float, loud_threshold: float) -> str:
    lines = []
    for seg in segments:
        if seg["end"] < a or seg["start"] > b:
            continue
        lo, hi = int(seg["start"]), max(int(seg["start"]) + 1, int(np.ceil(seg["end"])))
        peak = float(loud[lo:hi].max()) if loud.size and lo < loud.size else -120.0
        tag = " [ALTO]" if peak >= loud_threshold else ""
        lines.append(f"[{_fmt(seg['start'])}-{_fmt(seg['end'])}]{tag} {seg['text']}")
    return "\n".join(lines)


def snap(clip: dict, words: list[dict], min_s: float, max_s: float, duration: float) -> dict | None:
    """Snap LLM start/end to real word boundaries, pad slightly, enforce length."""
    try:
        start, end = float(clip["start"]), float(clip["end"])
    except (KeyError, TypeError, ValueError):
        return None
    if end <= start:
        return None
    if words:
        starts = [w["start"] for w in words]
        # first word starting at/after start-0.5
        i = next((k for k, st in enumerate(starts) if st >= start - 0.5), None)
        if i is None:
            return None
        start = words[i]["start"]
        # last word ending at/before end+0.8
        j = max((k for k, w in enumerate(words) if w["end"] <= end + 0.8), default=None)
        if j is None or j < i:
            return None
        end = words[j]["end"]
    start = max(0.0, start - 0.15)
    end = min(duration or end + 0.4, end + 0.4)
    if end - start > max_s:
        end = start + max_s
        if words:  # cut on a word end inside the limit
            ends = [w["end"] for w in words if start < w["end"] <= end]
            if ends:
                end = ends[-1] + 0.2
    if end - start < min_s:
        return None
    out = dict(clip)
    out["start"], out["end"] = round(start, 2), round(end, 2)
    return out


def clip_text(words: list[dict], start: float, end: float) -> str:
    return " ".join(w["word"] for w in words if start <= w["start"] < end).strip()


def find_highlights(s: Settings, transcript: dict, loud: np.ndarray, duration: float) -> list[dict]:
    cfg = s.section("clips")
    min_s, max_s = float(cfg.get("min_seconds", 15)), float(cfg.get("max_seconds", 75))
    segments, words = transcript.get("segments", []), transcript.get("words", [])
    windows = build_windows(segments, float(cfg.get("window_minutes", 12)) * 60)
    voiced = loud[loud > -60] if loud.size else loud
    threshold = float(np.percentile(voiced, 90)) if voiced.size else 0.0
    system = SYSTEM.format(
        creator=s.creator, min_s=int(min_s), max_s=int(max_s),
        target_s=int(cfg.get("target_seconds", 35)), max_n=int(cfg.get("max_per_window", 5)),
    )
    found: list[dict] = []
    for n, (a, b) in enumerate(windows):
        text = render_window(segments, loud, a, b, threshold)
        if len(text) < 200:
            continue
        log.info("highlights: window %d/%d (%.0f-%.0fs)", n + 1, len(windows), a, b)
        data = chat_json(s, system, f"Transcrição ({_fmt(a)}s a {_fmt(b)}s):\n{text}")
        for c in (data or {}).get("clips", []) if isinstance(data, dict) else []:
            c = snap(c, words, min_s, max_s, duration)
            if not c:
                continue
            try:
                c["score"] = float(c.get("score", 0))
            except (TypeError, ValueError):
                c["score"] = 0.0
            if c["score"] < float(cfg.get("min_score", 6)):
                continue
            c["hook"] = re.sub(r"\s+", " ", str(c.get("hook", "")).strip().strip('"'))[:80] or "Isto aconteceu na live"
            c["dialect"] = "pt-BR" if str(c.get("dialect", "")).lower().endswith("br") else "pt-PT"
            c["main_speaker"] = "guest" if c.get("main_speaker") == "guest" else "host"
            c["text"] = clip_text(words, c["start"], c["end"])
            found.append(c)
    return found
