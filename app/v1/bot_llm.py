"""Escalonamento dinâmico para LLM (Groq, Grok, OpenAI, Anthropic, Gemini, Vertex)."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.v1._sql import fetch_one

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Você é o Avaliador, assistente pedagógico da plataforma Parâmetro Pedagógico.
Ajude professores a interpretar dados de avaliação, presença e desempenho de turmas.
Responda em português do Brasil, de forma clara e prática (3–8 frases quando possível).

REGRAS OBRIGATÓRIAS:
- Use APENAS números, nomes, datas e fatos presentes no contexto JSON fornecido.
- O campo data_pack.schedules lista agendamentos (título, turma, start_date, status).
- O campo data_pack.schedule_facts traz first_applied, last_applied e next_upcoming.
- O campo data_pack.assessments resume cadernos (pendências, concluídos, turma).
- O campo data_pack.pedagogical_reports traz análise por componente (acurácia, variação pp,
  ação pedagógica intervir/orientar/desafiar) e critical_questions (questões com menor acerto).
- Se pedagogical_reports estiver vazio, oriente o professor a selecionar uma turma com provas aplicadas.
- O campo page_context descreve a tela aberta (tipo de relatório, avaliação, turma, resumo).
  Quando page_context estiver presente, responda EXCLUSIVAMENTE sobre esse relatório —
  ignore outros agendamentos, cadernos ou turmas que apareçam em data_pack.
- Se page_context.report_type for "schedule", use page_context.statistics e page_context.students
  como fonte única para totais, médias, conclusão e pendências desta prova.
- Se page_context.report_type for "macro", use page_context.statistics e page_context.assessments.
- Se a informação pedida NÃO estiver no contexto, responda explicitamente:
  "Não tenho essa informação nos dados disponíveis." — e NÃO invente valores.
- Nunca invente notas, médias, nomes de alunos, datas ou estatísticas.
- Sugira próximos passos pedagógicos (reforço, orientação, desafio) quando relevante."""

_PROVIDER_DEFAULTS: dict[str, str] = {
    "groq": "https://api.groq.com/openai/v1",
    "grok": "https://api.x.ai/v1",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "vertex": "projects/SEU_PROJECT/locations/us-central1",
}

_VERTEX_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def _env_llm_config() -> dict[str, Any] | None:
    groq_key = (settings.groq_api_key or "").strip()
    if groq_key:
        return {
            "provider": "groq",
            "model_name": settings.groq_model,
            "api_key": groq_key,
            "base_url": _PROVIDER_DEFAULTS["groq"],
            "is_active": True,
        }
    openai_key = (settings.assistant_openai_api_key or "").strip()
    if openai_key:
        return {
            "provider": "openai",
            "model_name": settings.assistant_openai_model,
            "api_key": openai_key,
            "base_url": settings.assistant_openai_base_url,
            "is_active": True,
        }
    return None


async def load_active_llm_config(db: AsyncSession) -> dict[str, Any] | None:
    """Fonte primária: linha ativa em bot_settings. .env só se BOT_LLM_SOURCE=env ou DB vazio."""
    source = "db"
    config: dict[str, Any] | None = None

    if settings.bot_llm_source == "env":
        config = _env_llm_config()
        source = "env"
    else:
        row = await fetch_one(
            db,
            """
            SELECT id, provider, model_name, api_key, base_url, is_active
            FROM bot_settings
            WHERE is_active = true
            ORDER BY updated_at DESC
            LIMIT 1
            """,
        )
        if row and str(row.get("api_key") or "").strip():
            config = dict(row)
        elif _env_llm_config():
            config = _env_llm_config()
            source = "env"

    if not config:
        return None
    normalized = normalize_llm_config(config)
    normalized["_config_source"] = source
    return normalized


def normalize_llm_config(config: dict[str, Any]) -> dict[str, Any]:
    """Corrige provider/base_url/model quando a chave não bate com o provedor salvo."""
    out = dict(config)
    api_key = str(out.get("api_key") or "").strip()
    provider = str(out.get("provider") or "openai")
    model = str(out.get("model_name") or "").strip()
    base = str(out.get("base_url") or "").strip()

    if api_key.startswith("gsk_"):
        out["provider"] = "groq"
        if not base or "x.ai" in base or "groq.com" not in base:
            out["base_url"] = _PROVIDER_DEFAULTS["groq"]
        if not model or model.lower().startswith("grok"):
            out["model_name"] = settings.groq_model
    elif api_key.startswith("xai-"):
        out["provider"] = "grok"
        if not base or "groq.com" in base:
            out["base_url"] = _PROVIDER_DEFAULTS["grok"]
        if not model or model.lower().startswith("llama"):
            out["model_name"] = "grok-2-1212"

    if not str(out.get("base_url") or "").strip():
        out["base_url"] = _PROVIDER_DEFAULTS.get(str(out.get("provider")), "")

    return out


def llm_config_source(config: dict[str, Any] | None) -> str:
    if not config:
        return "none"
    return str(config.get("_config_source") or settings.bot_llm_source)


async def verify_llm_connection(config: dict[str, Any] | None) -> tuple[bool, str | None]:
    """Ping leve na LLM ativa — usado em /v1/chat/status."""
    if not is_llm_configured(config):
        return False, "Nenhuma configuração LLM ativa"

    cfg = normalize_llm_config(config)
    provider = str(cfg.get("provider") or "openai")
    api_key = str(cfg["api_key"]).strip()
    model = str(cfg.get("model_name") or settings.groq_model)
    base = str(cfg.get("base_url") or _PROVIDER_DEFAULTS.get(provider, "")).rstrip("/")

    if provider == "anthropic":
        url = f"{base}/messages"
        payload = {
            "model": model,
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "ping"}],
        }
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
    elif provider == "vertex":
        try:
            url = _vertex_generate_url(base_url=base, model=model)
        except ValueError as exc:
            return False, str(exc)
        token = _vertex_access_token(api_key)
        payload = {
            "contents": [{"role": "user", "parts": [{"text": "ping"}]}],
            "generationConfig": {"maxOutputTokens": 8},
        }
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
            return True, None
        except Exception as exc:
            return False, format_llm_error(exc)
    else:
        url = f"{base}/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
        return True, None
    except Exception as exc:
        return False, format_llm_error(exc)


def is_llm_configured(config: dict[str, Any] | None) -> bool:
    return bool(config and str(config.get("api_key") or "").strip())


def prepare_llm_context(context: dict[str, Any]) -> dict[str, Any]:
    """Reduz data_pack conflitante quando há relatório aberto na tela."""
    page_context = context.get("page_context")
    if not isinstance(page_context, dict):
        return context

    report_type = str(page_context.get("report_type") or "")
    if report_type not in ("schedule", "pedagogical", "macro", "student"):
        return context

    narrowed = dict(context)
    data_pack = dict(context.get("data_pack") or {})

    if report_type == "schedule":
        schedule_id = page_context.get("schedule_id")
        if schedule_id and isinstance(data_pack.get("schedules"), list):
            data_pack["schedules"] = [
                item
                for item in data_pack["schedules"]
                if str(item.get("schedule_id") or "") == str(schedule_id)
            ]
        title = page_context.get("assessment_title")
        if title and isinstance(data_pack.get("assessments"), list):
            data_pack["assessments"] = [
                item for item in data_pack["assessments"] if item.get("title") == title
            ]
        data_pack["pedagogical_reports"] = []
        data_pack["pedagogical_report_count"] = 0

    if report_type in ("pedagogical", "student"):
        assessment_id = page_context.get("assessment_id")
        if assessment_id and isinstance(data_pack.get("assessments"), list):
            data_pack["assessments"] = [
                item
                for item in data_pack["assessments"]
                if str(item.get("assessment_id") or "") == str(assessment_id)
            ]
        if isinstance(data_pack.get("schedules"), list):
            title = page_context.get("assessment_title")
            if title:
                data_pack["schedules"] = [
                    item for item in data_pack["schedules"] if item.get("assessment_title") == title
                ]

    if report_type == "macro":
        data_pack["schedules"] = []
        data_pack["schedule_facts"] = {}
        data_pack["pedagogical_reports"] = []
        data_pack["pedagogical_report_count"] = 0

    narrowed["data_pack"] = data_pack
    narrowed["llm_focus"] = (
        "Responda apenas sobre o relatório descrito em page_context. "
        "Não agregue dados de outras avaliações ou turmas."
    )
    return narrowed


def _build_messages(
    *,
    user_message: str,
    context: dict[str, Any],
    history: list[dict[str, str]],
) -> list[dict[str, str]]:
    context_block = json.dumps(context, ensure_ascii=False, default=str)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"Contexto da turma/professor (JSON): {context_block}"},
    ]
    for item in history[-10:]:
        role = item.get("role")
        content = item.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": str(content)})
    messages.append({"role": "user", "content": user_message})
    return messages


async def _call_openai_compatible(
    *,
    config: dict[str, Any],
    messages: list[dict[str, str]],
) -> str:
    cfg = normalize_llm_config(config)
    provider = str(cfg.get("provider") or "openai")
    api_key = str(cfg["api_key"]).strip()
    model = str(cfg.get("model_name") or "gpt-4o-mini")
    base = str(cfg.get("base_url") or _PROVIDER_DEFAULTS.get(provider, "")).rstrip("/")
    url = f"{base}/chat/completions"

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
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
    content = (choices[0].get("message") or {}).get("content")
    if not content:
        raise RuntimeError("Conteúdo LLM ausente")
    return str(content).strip()


def format_llm_error(exc: Exception) -> str:
    """Mensagem curta para log/admin quando a LLM falha."""
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text[:240] if exc.response is not None else ""
        return f"HTTP {exc.response.status_code}: {body}" if body else str(exc)
    return str(exc)


async def _call_anthropic(
    *,
    config: dict[str, Any],
    messages: list[dict[str, str]],
) -> str:
    api_key = str(config["api_key"]).strip()
    model = str(config.get("model_name") or "claude-3-5-haiku-latest")
    base = str(config.get("base_url") or _PROVIDER_DEFAULTS["anthropic"]).rstrip("/")

    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    convo = [m for m in messages if m["role"] in ("user", "assistant")]

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{base}/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 900,
                "system": "\n\n".join(system_parts),
                "messages": [
                    {"role": m["role"], "content": m["content"]}
                    for m in convo
                    if m["role"] in ("user", "assistant")
                ],
            },
        )
        response.raise_for_status()
        payload = response.json()

    blocks = payload.get("content") or []
    texts = [b.get("text") for b in blocks if b.get("type") == "text" and b.get("text")]
    if not texts:
        raise RuntimeError("Resposta Anthropic vazia")
    return "\n".join(texts).strip()


def _to_gemini_contents(
    messages: list[dict[str, str]],
) -> tuple[str, list[dict[str, Any]]]:
    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    contents: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role not in ("user", "assistant") or not content:
            continue
        contents.append(
            {
                "role": "user" if role == "user" else "model",
                "parts": [{"text": str(content)}],
            }
        )
    return "\n\n".join(system_parts), contents


def _vertex_access_token(api_key: str) -> str:
    key = api_key.strip()
    if key.startswith("{"):
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_info(
            json.loads(key),
            scopes=[_VERTEX_SCOPE],
        )
        credentials.refresh(Request())
        return str(credentials.token)
    return key


def _vertex_generate_url(*, base_url: str, model: str) -> str:
    base = base_url.strip().strip("/")
    if not base.startswith("projects/") or "/locations/" not in base:
        raise ValueError(
            "Vertex base_url deve ser projects/PROJECT_ID/locations/REGION "
            "(ex.: projects/meu-projeto/locations/us-central1)"
        )
    location = base.split("/locations/", 1)[1].split("/")[0]
    host = f"https://{location}-aiplatform.googleapis.com/v1"
    return f"{host}/{base}/publishers/google/models/{model}:generateContent"


async def _call_vertex(
    *,
    config: dict[str, Any],
    messages: list[dict[str, str]],
) -> str:
    api_key = str(config["api_key"]).strip()
    model = str(config.get("model_name") or "gemini-2.0-flash-001")
    base = str(config.get("base_url") or _PROVIDER_DEFAULTS["vertex"]).strip()
    url = _vertex_generate_url(base_url=base, model=model)
    system_instruction, contents = _to_gemini_contents(messages)
    if not contents:
        raise RuntimeError("Nenhuma mensagem para Vertex")

    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {
            "temperature": 0.35,
            "maxOutputTokens": 900,
        },
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    token = _vertex_access_token(api_key)
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        body = response.json()

    candidates = body.get("candidates") or []
    if not candidates:
        raise RuntimeError("Resposta Vertex vazia")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    texts = [p.get("text") for p in parts if p.get("text")]
    if not texts:
        raise RuntimeError("Conteúdo Vertex ausente")
    return "\n".join(texts).strip()


async def generate_dynamic_llm_reply(
    db: AsyncSession,
    *,
    user_message: str,
    context: dict[str, Any],
    history: list[dict[str, str]],
) -> tuple[str, str]:
    config = await load_active_llm_config(db)
    if not is_llm_configured(config):
        raise RuntimeError("Nenhuma configuração LLM ativa")

    config = normalize_llm_config(config)
    llm_context = prepare_llm_context(context)
    messages = _build_messages(
        user_message=user_message,
        context=llm_context,
        history=history,
    )
    provider = str(config.get("provider") or "openai")

    if provider == "anthropic":
        reply = await _call_anthropic(config=config, messages=messages)
    elif provider == "vertex":
        reply = await _call_vertex(config=config, messages=messages)
    else:
        reply = await _call_openai_compatible(config=config, messages=messages)

    logger.info("bot.llm provider=%s model=%s", provider, config.get("model_name"))
    return reply, provider
