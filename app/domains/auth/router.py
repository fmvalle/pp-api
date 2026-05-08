from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext, get_auth_context, get_auth_context_optional, parse_forwarded_for
from app.db.session import get_db
from app.domains.auth.schemas import (
    AuthSessionRequest,
    AuthSessionResponse,
    FirebaseExchangeRequest,
    FirebaseExchangeResponse,
    LogoutRequest,
    RefreshRequest,
    SelectProfileRequest,
    TokenPairResponse,
)
from app.domains.auth.service import (
    create_auth_session,
    firebase_exchange,
    logout_session,
    refresh_tokens,
    select_profile_and_issue_tokens,
)

router = APIRouter()


@router.post("/session", response_model=AuthSessionResponse)
async def post_auth_session(
    body: AuthSessionRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_device_info: Annotated[str | None, Header(alias="X-Device-Info")] = None,
    x_forwarded_for: Annotated[str | None, Header(alias="X-Forwarded-For")] = None,
):
    """Autenticação completa: Firebase, `people`, lista de perfis, sessão em `app_sessions` e tokens da API."""
    return await create_auth_session(
        db,
        id_token=body.id_token,
        profile_id=body.profile_id,
        device_info=x_device_info,
        ip=parse_forwarded_for(x_forwarded_for) or (request.client.host if request.client else None),
        user_agent=request.headers.get("user-agent"),
    )


@router.post("/firebase/exchange", response_model=FirebaseExchangeResponse)
async def post_firebase_exchange(
    body: FirebaseExchangeRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    person_id, profiles = await firebase_exchange(db, body.id_token)
    return FirebaseExchangeResponse(person_id=person_id, profiles=profiles)


@router.post("/select-profile", response_model=TokenPairResponse)
async def post_select_profile(
    body: SelectProfileRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_device_info: Annotated[str | None, Header(alias="X-Device-Info")] = None,
    x_forwarded_for: Annotated[str | None, Header(alias="X-Forwarded-For")] = None,
):
    return await select_profile_and_issue_tokens(
        db,
        id_token=body.id_token,
        profile_id=body.profile_id,
        device_info=x_device_info,
        ip=parse_forwarded_for(x_forwarded_for) or (request.client.host if request.client else None),
        user_agent=request.headers.get("user-agent"),
    )


@router.post("/refresh", response_model=TokenPairResponse)
async def post_refresh(body: RefreshRequest, db: Annotated[AsyncSession, Depends(get_db)]):
    return await refresh_tokens(db, body.refresh_token)


@router.post("/logout")
async def post_logout(
    body: LogoutRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    ctx: Annotated[AuthContext | None, Depends(get_auth_context_optional)] = None,
):
    sid = ctx.session_id if ctx else None
    await logout_session(db, body.refresh_token, sid)
    return {"ok": True}
