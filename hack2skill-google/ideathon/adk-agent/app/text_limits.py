"""Token-budget and content-part helpers shared across the agent's model
call sites.

`token_upper_bound`/`truncate_to_token_cap` port `tokenUpperBound`/
`truncateToTokenCap` from server.ts:37-45. The app deliberately uses UTF-8
byte length as a conservative upper bound on model tokens: Gemini
tokenizers can fall back to byte-level tokens, so bounding bytes to the
requested token ceiling can never *undercount* input or output tokens.
"""

from __future__ import annotations

from typing import Any


def token_upper_bound(text: str) -> int:
    return len(text.encode("utf-8"))


def truncate_to_token_cap(text: str, cap: int) -> str:
    value = text
    while token_upper_bound(value) > cap and value:
        value = value[:-1]
    return value


def first_text_part(content: Any) -> str:
    """Returns the first non-empty `.text` from a `google.genai.types.
    Content`-shaped object's `.parts` list, or "" if there is none — the
    same "first text part" extraction app/main.py's direct model calls and
    app/tools/recall_tools.py's recalled `MemoryEntry`s both need, since
    both are `Content`-shaped (one from an LlmResponse, the other from
    ADK's MemoryEntry), previously hand-rolled twice (found by /simplify)."""
    parts = getattr(content, "parts", None) or []
    for part in parts:
        if getattr(part, "text", None):
            return part.text
    return ""
