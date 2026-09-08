"""Token-budget helpers.

Ports `tokenUpperBound` and `truncateToTokenCap` from server.ts:37-45. The
app deliberately uses UTF-8 byte length as a conservative upper bound on
model tokens: Gemini tokenizers can fall back to byte-level tokens, so
bounding bytes to the requested token ceiling can never *undercount* input or
output tokens.
"""

from __future__ import annotations


def token_upper_bound(text: str) -> int:
    return len(text.encode("utf-8"))


def truncate_to_token_cap(text: str, cap: int) -> str:
    value = text
    while token_upper_bound(value) > cap and value:
        value = value[:-1]
    return value
