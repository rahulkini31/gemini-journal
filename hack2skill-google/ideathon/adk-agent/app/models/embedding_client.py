"""Thin adapter around the Gemini text-embedding call.

Isolated in its own module, in the same spirit as app/security/app_check.py:
this wraps `google-genai`'s embedding call as remembered from the SDK's
current shape (`client.models.embed_content(model=..., contents=...,
config=types.EmbedContentConfig(output_dimensionality=...))` returning
`.embeddings[0].values`) but was not independently re-verified against live
SDK docs in this session — re-check the exact method/field names against the
installed `google-genai` version before pointing this at a real API key. All
real reads of embeddings happen only through this one adapter, so a
correction here is a one-file fix.
"""

from __future__ import annotations

from typing import Protocol, Sequence

from app.config import MEMORY_EMBEDDING_DIMENSIONS, MEMORY_EMBEDDING_MODEL


class EmbeddingClient(Protocol):
    def embed(self, text: str) -> Sequence[float]: ...


class GeminiEmbeddingClient:
    """Production adapter. Requires GEMINI_API_KEY; never logs `text`."""

    def __init__(self, *, api_key_env: str = "GEMINI_API_KEY", model: str = MEMORY_EMBEDDING_MODEL):
        self._api_key_env = api_key_env
        self._model = model
        self._client = None  # lazily constructed so importing this module never requires a key

    def _client_or_raise(self):
        if self._client is None:
            import os

            from google import genai

            api_key = os.environ.get(self._api_key_env)
            if not api_key:
                raise RuntimeError("GEMINI_API_KEY is not configured.")
            self._client = genai.Client(api_key=api_key)
        return self._client

    def embed(self, text: str) -> Sequence[float]:
        from google.genai import types

        client = self._client_or_raise()
        response = client.models.embed_content(
            model=self._model,
            contents=text,
            config=types.EmbedContentConfig(output_dimensionality=MEMORY_EMBEDDING_DIMENSIONS),
        )
        return response.embeddings[0].values
