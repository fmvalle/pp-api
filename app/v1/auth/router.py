from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext, get_auth_context_optional, parse_forwarded_for
from app.db.session import get_db
from app.domains.auth.service import logout_session
from app.v1.auth.schemas import (
    FirebaseExchangeRequestV1,
    FirebaseExchangeResponseV1,
    LogoutRequestV1,
    RefreshRequestV1,
    SelectProfileRequestV1,
    SelectProfileResponseV1,
    SignInRequestV1,
    TokenPairResponseV1,
)
from app.v1.auth.service import (
    firebase_exchange_v1,
    firebase_sign_in_with_password_v1,
    refresh_v1,
    select_profile_v1,
)

router = APIRouter()


@router.post(
    "/sign-in",
    response_model=FirebaseExchangeResponseV1,
    summary="Sign-in com email e senha",
    description=(
        "Autentica no **Firebase** via Identity Toolkit (REST) usando `FIREBASE_WEB_API_KEY` e "
        "devolve o **mesmo corpo** que `POST /v1/auth/firebase/exchange` (`person`, `profiles`, "
        "`bootstrap_token`). Depois use `POST /v1/auth/select-profile` para obter tokens da API.\n\n"
        "Compatível com clientes que esperam um único passo com credenciais na API (ex.: "
        "`POST /api/v1/auth/sign-in` — ver rota espelhada na raiz da app)."
    ),
)
async def post_sign_in_v1(body: SignInRequestV1, db: Annotated[AsyncSession, Depends(get_db)]):
    return await firebase_sign_in_with_password_v1(db, body.email, body.password)


@router.post(
    "/firebase/exchange",
    response_model=FirebaseExchangeResponseV1,
    summary="Trocar ID token Firebase",
    description=(
        "Valida o `id_token` do Firebase (mesmo `FIREBASE_PROJECT_ID` que o cliente) e devolve "
        "`person`, `profiles`, `bootstrap_token` e contexto. **Não** devolve ainda `access_token` "
        "da API — use `POST /v1/auth/select-profile`."
    ),
)
async def post_firebase_exchange_v1(body: FirebaseExchangeRequestV1, db: Annotated[AsyncSession, Depends(get_db)]):
    return await firebase_exchange_v1(db, body.id_token)


@router.post(
    "/select-profile",
    response_model=SelectProfileResponseV1,
    summary="Seleccionar perfil e obter tokens",
    description=(
        "Envie `profile_id` e `bootstrap_token` (recomendado) **ou** `id_token`. "
        "Resposta inclui `access_token`, `refresh_token` e `active_profile`."
    ),
)
async def post_select_profile_v1(
    body: SelectProfileRequestV1,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_device_info: Annotated[str | None, Header(alias="X-Device-Info")] = None,
    x_forwarded_for: Annotated[str | None, Header(alias="X-Forwarded-For")] = None,
):
    return await select_profile_v1(
        db,
        profile_id=body.profile_id,
        bootstrap_token=body.bootstrap_token,
        id_token=body.id_token,
        device_info=x_device_info,
        ip=parse_forwarded_for(x_forwarded_for) or (request.client.host if request.client else None),
        user_agent=request.headers.get("user-agent"),
    )


@router.post(
    "/refresh",
    response_model=TokenPairResponseV1,
    summary="Renovar access token",
    description="Corpo JSON com `refresh_token`; devolve novo par de tokens.",
)
async def post_refresh_v1(body: RefreshRequestV1, db: Annotated[AsyncSession, Depends(get_db)]):
    return await refresh_v1(db, body.refresh_token)


@router.post(
    "/logout",
    summary="Encerrar sessão",
    description="Invalida a sessão de refresh. Envie `refresh_token` e/ou `Authorization: Bearer` com access token válido.",
)
async def post_logout_v1(
    body: LogoutRequestV1,
    db: Annotated[AsyncSession, Depends(get_db)],
    ctx: Annotated[AuthContext | None, Depends(get_auth_context_optional)] = None,
):
    if not body.refresh_token and not ctx:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "logout requer refresh_token ou Authorization Bearer válido",
        )
    await logout_session(db, body.refresh_token, ctx.session_id if ctx else None)
    return {"ok": True}


