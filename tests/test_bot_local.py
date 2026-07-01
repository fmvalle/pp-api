"""Testes da camada local (fuzzy matching)."""

from app.v1.bot_local import match_local_intent, should_use_local_intent, LocalMatch


def test_match_local_intent_high_confidence():
    intents = [
        {
            "intent_key": "doc_presenca",
            "patterns": ["lista de presença", "registrar presença"],
            "response_template": "Use o menu Presença.",
            "min_score": 85.0,
        }
    ]
    result = match_local_intent("Como registrar presença na turma?", intents)
    assert result is not None
    assert result.intent_key == "doc_presenca"
    assert result.confidence >= 85.0
    assert "Presença" in result.reply


def test_match_local_intent_low_confidence_returns_none():
    intents = [
        {
            "intent_key": "doc_presenca",
            "patterns": ["lista de presença"],
            "response_template": "ok",
            "min_score": 95.0,
        }
    ]
    result = match_local_intent("explique a teoria da relatividade", intents)
    assert result is None


def test_should_use_local_intent_skips_interpretive():
    match = LocalMatch(
        intent_key="doc_pedagogical_report",
        reply="template",
        confidence=100.0,
    )
    assert should_use_local_intent(match) is False


def test_should_use_local_intent_requires_high_score_for_nav():
    low = LocalMatch(intent_key="doc_presenca", reply="ok", confidence=88.0)
    high = LocalMatch(intent_key="doc_presenca", reply="ok", confidence=95.0)
    assert should_use_local_intent(low) is False
    assert should_use_local_intent(high) is True
