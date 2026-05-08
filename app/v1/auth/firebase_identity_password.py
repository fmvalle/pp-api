"""Troca email+senha por ID token via Firebase Identity Toolkit (REST).

O Admin SDK não faz sign-in com password; o cliente web usa o SDK ou esta REST API.
Documentação: https://firebase.google.com/docs/reference/rest/auth#section-sign-in-email-password
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from fastapi import HTTPException, status


def fetch_firebase_id_token_from_password(*, email: str, password: str, web_api_key: str) -> str:
    """Chama `signInWithPassword` e devolve o JWT `idToken` ou levanta HTTPException."""
    key = (web_api_key or "").strip()
    if not key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "sign-in com email/senha requer FIREBASE_WEB_API_KEY (chave Web do mesmo projeto Firebase).",
        )
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={key}"
    payload = json.dumps(
        {
            "email": (email or "").strip(),
            "password": password or "",
            "returnSecureToken": True,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        msg = _firebase_rest_error_message(e)
        code = status.HTTP_401_UNAUTHORIZED
        if e.code in (400, 401, 403):
            code = status.HTTP_401_UNAUTHORIZED
        raise HTTPException(code, msg) from e
    except urllib.error.URLError as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Falha de rede ao contactar Firebase Identity Toolkit: {e!s}",
        ) from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Resposta inválida do Firebase Identity Toolkit.",
        ) from e

    id_token = data.get("idToken") or data.get("id_token")
    if not id_token or not isinstance(id_token, str):
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Resposta Firebase sem idToken.",
        )
    return id_token.strip()


def _firebase_rest_error_message(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body)
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err, dict):
            msg = err.get("message")
            if isinstance(msg, str) and msg:
                if "INVALID_PASSWORD" in msg or "INVALID_LOGIN_CREDENTIALS" in msg:
                    return "Email ou senha inválidos."
                if "EMAIL_NOT_FOUND" in msg:
                    return "Email ou senha inválidos."
                if "USER_DISABLED" in msg:
                    return "Conta desactivada."
                if "TOO_MANY_ATTEMPTS_TRY_LATER" in msg:
                    return "Demasiadas tentativas. Tente mais tarde."
                return msg
    except Exception:
        pass
    return "Falha na autenticação Firebase (email/senha)."
