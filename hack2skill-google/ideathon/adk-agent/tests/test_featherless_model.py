"""Ports the intent of the old test_fallback_ladder.py to the new
single-model setup: there is no ladder to sequence anymore, but the model
builder's configuration and failure mode are still worth verifying directly
against the real installed google-adk[extensions] package.
"""

from __future__ import annotations

import pytest

from app.config import FEATHERLESS_API_BASE, FEATHERLESS_MODEL
from app.models.featherless_llm import build_gemma_model


def test_missing_api_key_raises_immediately_not_deep_inside_a_call(monkeypatch):
    monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="FEATHERLESS_API_KEY is not configured"):
        build_gemma_model()


def test_builds_a_lite_llm_pointed_at_featherless_with_the_configured_model(monkeypatch):
    monkeypatch.setenv("FEATHERLESS_API_KEY", "test-key")
    model = build_gemma_model()
    # LiteLlm's own `model` field is "openai/<model-id>" (the "openai/"
    # prefix tells LiteLLM to use the OpenAI-compatible chat-completions
    # wire format against api_base, verified against the installed
    # google-adk[extensions] package).
    assert model.model == f"openai/{FEATHERLESS_MODEL}"
    assert model._additional_args["api_base"] == FEATHERLESS_API_BASE
    assert model._additional_args["api_key"] == "test-key"


def test_respects_a_custom_api_key_env_var_name(monkeypatch):
    monkeypatch.setenv("SOME_OTHER_KEY", "another-key")
    model = build_gemma_model(api_key_env="SOME_OTHER_KEY")
    assert model._additional_args["api_key"] == "another-key"
