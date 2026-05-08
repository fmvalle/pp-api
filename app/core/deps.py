from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.session import get_db
from app.domains.auth.models import AppSession

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthContext:
    session_id: UUID
    person_id: UUID
    active_profile_id: UUID
    role: str
    school_id: UUID | None


async def get_auth_context(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AuthContext:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")

    try:
        payload = decode_access_token(creds.credentials)
    except Exception as exc:  # noqa: BLE001 — PyJWT errors
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc

    if payload.get("typ") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token type")

    from datetime import datetime, timezone

    sid = UUID(payload["sid"])
    res = await db.execute(select(AppSession).where(AppSession.id == sid, AppSession.revoked_at.is_(None)))
    row = res.scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session revoked or unknown")

    now = datetime.now(timezone.utc)
    if row.expires_at <= now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")

    pid = UUID(payload["sub"])
    prf = UUID(payload["prf"])
    if row.person_id != pid or row.active_profile_id != prf:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token does not match session")

    return AuthContext(
        session_id=sid,
        person_id=pid,
        active_profile_id=prf,
        role=str(payload["role"]),
        school_id=UUID(payload["sch"]) if payload.get("sch") else None,
    )


def parse_forwarded_for(x_forwarded_for: str | None) -> str | None:
    if not x_forwarded_for:
        return None
    return x_forwarded_for.split(",")[0].strip() or None


async def get_auth_context_optional(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AuthContext | None:
    if creds is None or creds.scheme.lower() != "bearer":
        return None
    try:
        return await get_auth_context(creds, db)
    except HTTPException:
        return None  # ex.: token inválido no logout — ainda permite revogar via refresh_token
