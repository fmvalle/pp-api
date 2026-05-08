import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import jwt

from app.core.config import settings


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_refresh_token(raw: str) -> str:
    payload = (settings.refresh_token_pepper + raw).encode()
    return hashlib.sha256(payload).hexdigest()


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def create_access_token(
    *,
    session_id: UUID,
    person_id: UUID,
    profile_id: UUID,
    role: str,
    school_id: UUID | None,
) -> str:
    now = _utcnow()
    exp = now + timedelta(minutes=settings.access_token_ttl_minutes)
    claims: dict[str, Any] = {
        "typ": "access",
        "sid": str(session_id),
        "sub": str(person_id),
        "prf": str(profile_id),
        "role": role,
        "sch": str(school_id) if school_id else None,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def create_bootstrap_token(*, person_id: UUID) -> str:
    """JWT curto pós-exchange; usado em POST /v1/auth/select-profile (sem reenviar Firebase ID token)."""
    now = _utcnow()
    exp = now + timedelta(minutes=settings.bootstrap_token_ttl_minutes)
    claims: dict[str, Any] = {
        "typ": "bootstrap",
        "sub": str(person_id),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_bootstrap_token(token: str) -> UUID:
    payload = jwt.decode(token.strip(), settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    if payload.get("typ") != "bootstrap":
        raise ValueError("Invalid bootstrap token type")
    return UUID(payload["sub"])


def session_expires_at() -> datetime:
    return _utcnow() + timedelta(days=settings.refresh_token_ttl_days)
