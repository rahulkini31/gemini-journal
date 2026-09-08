"""Builds the single Featherless-hosted Gemma model this agent uses.

Replaces the removed `fallback_llm.py::FallbackLadderLlm`: this project
originally required Gemini's exact 4-model fallback ladder
(docs/ai-studio-production-directives.md), but now uses a single model —
Gemma 3 27B, served through Featherless's OpenAI-compatible API — so there
is no ladder to build. ADK's own `LiteLlm` connector talks to any
OpenAI-compatible endpoint directly; no custom `BaseLlm` subclass is needed
for this.

Verified against the actually-installed package, not assumed: importing
`google.adk.models.lite_llm.LiteLlm` raises `ImportError` unless
`google-adk[extensions]` is installed (a real, non-obvious requirement —
see pyproject.toml), and `LiteLlm(model="openai/<model>", api_base=...,
api_key=...)` constructs and stores those as pass-through kwargs to
litellm's own `completion()` call, matching the ADK docs' documented usage
for third-party OpenAI-compatible endpoints.
"""

from __future__ import annotations

import os

from google.adk.models.lite_llm import LiteLlm

from app.config import FEATHERLESS_API_BASE, FEATHERLESS_MODEL


def build_gemma_model(*, api_key_env: str = "FEATHERLESS_API_KEY") -> LiteLlm:
    """Raises immediately if the API key is missing, rather than failing
    later inside a deep litellm HTTP call — the same eager-check pattern
    `GeminiEmbeddingClient` uses for `GEMINI_API_KEY`."""
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(f"{api_key_env} is not configured.")
    return LiteLlm(
        model=f"openai/{FEATHERLESS_MODEL}",
        api_base=FEATHERLESS_API_BASE,
        api_key=api_key,
    )
