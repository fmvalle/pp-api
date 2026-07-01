"""Chamada opcional a LLM (OpenAI-compatible) para o Avaliador."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Você é o Avaliador, assistente pedagógico da plataforma Parâmetro Pedagógico.
Ajude professores a interpretar dados de avaliação, presença e desempenho de turmas.
Responda em português do Brasil, de forma clara e prática (3–8 frases quando possível).
Não invente números: use apenas o contexto JSON fornecido.
Sugira próximos passos pedagógicos (reforço, orientação, desafio) quando relevante."""


def is_llm_enabled() -> bool:
    return bool((settings.assistant_openai_api_key or "").strip())


def _build_messages(
    *,
    user_message: str,
    context: dict[str, Any],
    history: list[dict[str, str]],
) -> list[dict[str, str]]:
    context_block = json.dumps(context, ensure_ascii=False, default=str)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "system",
            "content": f"Contexto da turma/professor (JSON): {context_block}",
        },
    ]
    for item in history[-10:]:
        role = item.get("role")
        content = item.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": str(content)})
    messages.append({"role": "user", "content": user_message})
    return messages


async def generate_llm_reply(
    *,
    user_message: str,
    context: dict[str, Any],
    history: list[dict[str, str]],
) -> str:
    api_key = (settings.assistant_openai_api_key or "").strip()
    if not api_key:
        raise RuntimeError("LLM não configurado")

    messages = _build_messages(
        user_message=user_message,
        context=context,
        history=history,
    )
    base = settings.assistant_openai_base_url.rstrip("/")
    url = f"{base}/chat/completions"

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.assistant_openai_model,
                "messages": messages,
                "temperature": 0.35,
                "max_tokens": 900,
            },
        )
        response.raise_for_status()
        payload = response.json()

    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("Resposta LLM vazia")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not content:
        raise RuntimeError("Conteúdo LLM ausente")
    return str(content).strip()
