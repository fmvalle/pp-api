"""Testes helpers LLM (Gemini/Vertex)."""

import pytest

from app.v1.bot_llm import _to_gemini_contents, _vertex_generate_url


def test_to_gemini_contents_maps_roles():
    system, contents = _to_gemini_contents(
        [
            {"role": "system", "content": "Instrução"},
            {"role": "user", "content": "Olá"},
            {"role": "assistant", "content": "Oi"},
        ]
    )
    assert system == "Instrução"
    assert contents == [
        {"role": "user", "parts": [{"text": "Olá"}]},
        {"role": "model", "parts": [{"text": "Oi"}]},
    ]


def test_vertex_generate_url():
    url = _vertex_generate_url(
        base_url="projects/demo/locations/us-central1",
        model="gemini-2.0-flash-001",
    )
    assert url == (
        "https://us-central1-aiplatform.googleapis.com/v1/"
        "projects/demo/locations/us-central1/publishers/google/models/"
        "gemini-2.0-flash-001:generateContent"
    )


def test_normalize_llm_config_gsk_to_groq():
    from app.v1.bot_llm import normalize_llm_config

    cfg = normalize_llm_config(
        {
            "provider": "grok",
            "model_name": "grok-2-latest",
            "api_key": "gsk_test_key",
            "base_url": "https://api.x.ai/v1",
        }
    )
    assert cfg["provider"] == "groq"
    assert cfg["base_url"] == "https://api.groq.com/openai/v1"
    assert cfg["model_name"] == "llama-3.3-70b-versatile"


def test_normalize_llm_config_preserves_groq_model_from_db():
    from app.v1.bot_llm import normalize_llm_config

    cfg = normalize_llm_config(
        {
            "provider": "groq",
            "model_name": "llama-3.1-8b-instant",
            "api_key": "gsk_test_key",
            "base_url": "https://api.groq.com/openai/v1",
        }
    )
    assert cfg["model_name"] == "llama-3.1-8b-instant"

