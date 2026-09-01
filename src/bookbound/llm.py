"""OpenAI-compatible chat client. Used for Featherless; no SDK required."""
from __future__ import annotations

import json
import re

from .http import ApiError, post_json


class LLMError(RuntimeError):
    pass


def chat_json(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 800,
    temperature: float = 0.2,
) -> dict:
    """Chat completion, parsed as JSON.

    Featherless returns 503 on a cold model; ``post_json`` does not retry, so the
    caller decides. For an autonomous agent a NO TRADE on LLM failure is the
    correct fallback - never a trade.
    """
    if not api_key:
        raise LLMError("no API key configured")

    # Reasoning models (GLM-5.2) spend hundreds of tokens before emitting the
    # answer; a small budget returns an empty string, not an error.
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return _once(base_url, api_key, model, system, user, max_tokens, temperature)
        except LLMError as exc:
            # 503 = cold model or at capacity; 1010 = transient edge rejection.
            # Both are worth one retry. A genuinely gated model (403 "gated")
            # will not recover, so do not spin on it.
            text = str(exc)
            if attempt < 2 and ("503" in text or "1010" in text or "capacity" in text):
                import time as _time
                _time.sleep(2.0 * (attempt + 1))
                last_error = exc
                continue
            raise
    raise LLMError(str(last_error))


def _once(base_url, api_key, model, system, user, max_tokens, temperature) -> dict:
    try:
        payload = post_json(
            f"{base_url}/chat/completions",
            {"Authorization": f"Bearer {api_key}"},
            {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
    except ApiError as exc:
        raise LLMError(f"{model}: {exc}") from exc

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise LLMError(f"malformed response: {str(payload)[:200]}") from exc

    if not (content or "").strip():
        raise LLMError(f"{model}: empty response (raise max_tokens for reasoning models)")

    return _extract_json(content)


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of a model response, fenced or not."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"no JSON object in response: {text[:200]}")
        text = text[start : end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"invalid JSON: {text[:200]}") from exc
