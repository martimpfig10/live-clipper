"""Thin wrapper over any OpenAI-compatible chat endpoint, returning parsed JSON."""
from __future__ import annotations

import json
import logging
import re
import time

from .config import Settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def parse_json(text: str):
    text = text.strip()
    # strip ```json fences / reasoning preambles
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        a, b = text.find(open_c), text.rfind(close_c)
        if a != -1 and b > a:
            try:
                return json.loads(text[a : b + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"Model did not return JSON: {text[:300]}")


def chat_json(s: Settings, system: str, user: str, temperature: float = 0.4, max_tokens: int = 4000):
    from openai import OpenAI

    client = OpenAI(base_url=s.llm_base_url, api_key=s.llm_api_key or "missing", max_retries=0)
    delay = 5.0
    last: Exception | None = None
    use_json_mode = True
    for attempt in range(8):
        try:
            kwargs = dict(
                model=s.llm_model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if use_json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            resp = client.chat.completions.create(**kwargs)
            return parse_json(resp.choices[0].message.content or "")
        except LLMError as e:
            last = e
            log.warning("LLM returned non-JSON (attempt %d)", attempt + 1)
        except Exception as e:
            last = e
            status = getattr(e, "status_code", None)
            if status == 400 and use_json_mode:
                use_json_mode = False  # provider doesn't support response_format; retry without
                continue
            if status in (401, 403, 404):
                raise
            log.warning("LLM call failed (%s); retrying in %.0fs", e, delay)
            time.sleep(delay)
            delay = min(delay * 2, 120)
    raise LLMError(f"LLM failed after retries: {last}")
