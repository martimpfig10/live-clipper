"""Write the TikTok description (legenda) for a clip."""
from __future__ import annotations

import re

from .config import Settings
from .llm import chat_json

SYSTEM = """Escreves descrições (legendas) de TikTok para clips das lives do criador "{creator}".
Regras:
- Escreve em {dialect_name}. {dialect_hint}
- 1 a 2 frases curtas. A primeira frase tem de dar vontade de ver até ao fim (curiosidade), sem spoilar o remate.
- Termina com uma pergunta simples que puxe comentários.
- No máximo 1 emoji. Nada de "link na bio", nada de promessas, nada inventado que não esteja na transcrição.
- Entre {h_min} e {h_max} hashtags relevantes, em minúsculas, misturando público de Portugal e do Brasil \
quando fizer sentido (ex.: #portugal #brasil #humor #live). Não incluas {base}.
Responde APENAS com JSON: {{"description": "...", "hashtags": ["#...", "#..."]}}"""

DIALECT = {
    "pt-PT": ("português de Portugal", "Usa 'tu', 'estás', 'giro', 'malta' — nada de 'você' nem gerúndios brasileiros."),
    "pt-BR": ("português do Brasil", "Usa 'você', 'cara', 'muito bom' — tom natural brasileiro."),
}


def clean_hashtags(tags, base: list[str], h_max: int) -> list[str]:
    out: list[str] = []
    for t in list(base) + list(tags or []):
        t = "#" + re.sub(r"[^\w]", "", str(t).lstrip("#").lower(), flags=re.UNICODE)
        if len(t) > 1 and t not in out:
            out.append(t)
    return out[: len(base) + h_max]


def compose(description: str, hashtags: list[str], credit: str) -> str:
    parts = [description.strip()]
    if credit:
        parts.append(credit.strip())
    if hashtags:
        parts.append(" ".join(hashtags))
    return "\n\n".join(p for p in parts if p)


def write_caption(s: Settings, hook: str, text: str, dialect: str) -> str:
    cfg = s.section("captions")
    base = list(cfg.get("base_hashtags", []))
    name, hint = DIALECT.get(dialect, DIALECT["pt-PT"])
    system = SYSTEM.format(
        creator=s.creator, dialect_name=name, dialect_hint=hint,
        h_min=cfg.get("hashtags_min", 3), h_max=cfg.get("hashtags_max", 6), base=" ".join(base) or "-",
    )
    user = f"Título do vídeo: {hook}\n\nTranscrição do clip:\n{text[:3000]}"
    data = chat_json(s, system, user, temperature=0.8, max_tokens=1500)
    desc = str((data or {}).get("description", "")).strip() if isinstance(data, dict) else ""
    tags = (data or {}).get("hashtags", []) if isinstance(data, dict) else []
    return compose(desc or hook, clean_hashtags(tags, base, int(cfg.get("hashtags_max", 6))), s.credit_line)
