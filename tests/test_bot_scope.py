"""Testes de escopo do Avaliador por perfil."""

from app.v1._scope import can_use_assistant, resolve_bot_audience


def test_resolve_bot_audience_teacher():
    assert resolve_bot_audience("teacher") == "teacher"
    assert resolve_bot_audience("professor") == "teacher"


def test_resolve_bot_audience_platform_admin():
    assert resolve_bot_audience("platform_admin") == "platform_admin"
    assert resolve_bot_audience("admin") == "platform_admin"


def test_resolve_bot_audience_school_admin():
    assert resolve_bot_audience("school_admin") == "school_admin"


def test_can_use_assistant():
    assert can_use_assistant("teacher") is True
    assert can_use_assistant("platform_admin") is True
    assert can_use_assistant("admin") is True
    assert can_use_assistant("school_admin") is False
    assert can_use_assistant("student") is False
