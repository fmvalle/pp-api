"""Camada local: fuzzy matching de intenções (rapidfuzz) + templates."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz, process
from sqlalchemy.ext.asyncio import AsyncSession

from app.v1._sql import fetch_all

# Intenções interpretativas: preferir LLM (com data_pack) em vez de template estático.
LLM_PREFERRED_INTENTS = frozenset(
    {
        "doc_pedagogical_report",
        "doc_macro",
        "doc_tri",
        "doc_questions_modal",
        "doc_schedule_report",
        "doc_student_report",
        "doc_neurotypical",
        "doc_reinforcement",
    }
)

# Navegação pura: resposta local quando confiança alta (economiza tokens).
LOCAL_NAV_MIN_SCORE = 92.0

_BUILTIN_INTENTS: list[dict[str, Any]] = [
    {
        "intent_key": "doc_assessments",
        "title": "Avaliações",
        "patterns": [
            "lista de avaliações",
            "minhas avaliações",
            "agendar avaliação",
            "cadernos",
        ],
        "response_template": (
            "Em **Avaliações** (`/teacher/assessments`) você vê cadernos e macros "
            "das suas turmas, com links para relatório por agendamento e presença."
        ),
        "min_score": 85.0,
    },
]


def normalize_text(text: str) -> str:
    lowered = text.strip().lower()
    normalized = unicodedata.normalize("NFKD", lowered)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


async def load_active_intents(db: AsyncSession, *, audience: str) -> list[dict[str, Any]]:
    rows = await fetch_all(
        db,
        """
        SELECT intent_key, title, patterns, response_template, min_score
        FROM bot_intents
        WHERE is_active = true
          AND (
            CAST(:audience AS text) = ANY(audiences)
            OR 'all' = ANY(audiences)
          )
        ORDER BY intent_key
        """,
        {"audience": audience},
    )
    intents: list[dict[str, Any]] = []
    for row in rows:
        patterns = row.get("patterns")
        if isinstance(patterns, str):
            patterns = json.loads(patterns)
        if not isinstance(patterns, list):
            patterns = []
        intents.append(
            {
                "intent_key": row["intent_key"],
                "title": row["title"],
                "patterns": [str(p) for p in patterns],
                "response_template": row["response_template"],
                "min_score": float(row.get("min_score") or DEFAULT_MIN_SCORE),
            }
        )
    if not intents:
        intents = list(_BUILTIN_INTENTS)
    return intents


@dataclass(frozen=True)
class LocalMatch:
    intent_key: str
    reply: str
    confidence: float


def match_local_intent(message: str, intents: list[dict[str, Any]]) -> LocalMatch | None:
    normalized = normalize_text(message)
    if not normalized:
        return None

    choices: dict[str, dict[str, Any]] = {}
    for intent in intents:
        for pattern in intent.get("patterns") or []:
            key = normalize_text(str(pattern))
            if key:
                choices[key] = intent

    if not choices:
        return None

    result = process.extractOne(
        normalized,
        choices.keys(),
        scorer=fuzz.token_set_ratio,
        score_cutoff=0,
    )
    if not result:
        return None

    matched_text, score, _index = result
    intent = choices[matched_text]
    min_score = float(intent.get("min_score") or DEFAULT_MIN_SCORE)
    if score < min_score:
        return None

    return LocalMatch(
        intent_key=str(intent["intent_key"]),
        reply=str(intent["response_template"]),
        confidence=float(score),
    )


def should_use_local_intent(match: LocalMatch) -> bool:
    if match.intent_key in LLM_PREFERRED_INTENTS:
        return False
    return match.confidence >= LOCAL_NAV_MIN_SCORE


def looks_like_data_question(message: str) -> bool:
    text = normalize_text(message)
    data_markers = (
        r"\b(nota|notas|score|media|desempenho|proficiencia|proficiência)\b",
        r"\b(aluno|estudante)\b.*\b(menor|maior|pior|melhor|piores|melhores|media)\b",
        r"\b(menor|maior|pior|melhor)\b.*\b(nota|score|media|desempenho)\b",
        r"\b(menor|maior|pior|melhor)\b.*\b(aluno|estudante)\b",
        r"\bqual\s+aluno\b.*\b(media|nota|desempenho)\b",
        r"\bquantos\b.*\b(alunos|pendentes|concluid)\b",
    )
    return any(re.search(pattern, text) for pattern in data_markers)
