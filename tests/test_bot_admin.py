"""Testes admin bot (máscara de API key)."""

from app.v1.bot_admin_router import _mask_api_key


def test_mask_api_key_short():
    assert _mask_api_key("abc") == "****"


def test_mask_api_key_shows_last_four():
    assert _mask_api_key("xai-secret-key-1234").endswith("1234")
    assert _mask_api_key("xai-secret-key-1234").startswith("*")
