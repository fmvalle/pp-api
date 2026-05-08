from contextlib import asynccontextmanager

from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.firebase import firebase_credential_diagnostic
from app.db.session import get_db
from app.domains.auth.router import router as auth_router
from app.domains.me.router import router as me_router
from app.v1.auth.schemas import FirebaseExchangeResponseV1, SignInRequestV1
from app.v1.auth.service import firebase_sign_in_with_password_v1
from app.v1.router import router as v1_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Firebase inicializa em lazy (ensure_firebase_initialized) na primeira rota que precise.
    yield


_OPENAPI_TAGS_METADATA = [
    {
        "name": "Health",
        "description": "Disponibilidade. `GET /health?firebase=1` expõe diagnóstico **sem segredos** da config Firebase Admin (Cloud Run / local).",
    },
    {
        "name": "auth",
        "description": "Sessão legada no prefixo `/auth` (ex.: `POST /auth/session`). Preferir **v1-auth** para novos clientes.",
    },
    {"name": "me", "description": "Perfil e sessão no prefixo `/me` (Bearer)."},
    {
        "name": "v1-auth",
        "description": "Autenticação v1: `sign-in` (email+senha na API) ou `firebase/exchange` (id_token) → `bootstrap_token` → `select-profile` → tokens.",
    },
    {"name": "v1-me", "description": "Contexto e perfis sob `/v1/me`."},
    {"name": "v1-profiles", "description": "Perfis e escolas."},
    {"name": "v1-catalog", "description": "Catálogo (disciplinas, séries, etc.)."},
    {"name": "v1-directory", "description": "Directório (pessoas, escolas)."},
    {"name": "v1-assessments", "description": "Avaliações, presenças, relatórios."},
    {"name": "v1-exam-reports", "description": "Relatórios de exame."},
    {"name": "v1-hardcut", "description": "Compatibilidade / readiness."},
]

app = FastAPI(
    title="PP API",
    version="0.1.0",
    lifespan=lifespan,
    description=(
        "API HTTP da plataforma (FastAPI).\n\n"
        "**Testar na consola:** [Swagger UI](/docs) · [ReDoc](/redoc) · "
        "[OpenAPI JSON](/openapi.json) (importar no Postman: *Import → Link* com `…/openapi.json`).\n\n"
        "**Auth v1:** `POST /v1/auth/sign-in` com `email` + `password` (requer `FIREBASE_WEB_API_KEY`) **ou** "
        "`POST /v1/auth/firebase/exchange` com `id_token` → depois `POST /v1/auth/select-profile` "
        "com `bootstrap_token` + `profile_id` → usar `access_token` nas rotas com Bearer. "
        "Alias: `POST /api/v1/auth/sign-in` (mesmo corpo)."
    ),
    openapi_tags=_OPENAPI_TAGS_METADATA,
)


class PrivateNetworkAccessMiddleware(BaseHTTPMiddleware):
    """Responde ao preflight do Chrome (Private Network Access) para APIs em localhost/rede privada.

    Sem este cabeçalho, pedidos POST com corpo a partir de algumas origens falham no browser
    com `Failed to fetch` mesmo com CORS aparentemente correto.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.method != "OPTIONS":
            return response
        raw = request.headers.get("access-control-request-private-network")
        if raw is not None and str(raw).lower() == "true":
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_origin_regex=settings.cors_allow_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Deve ficar por último para envolver a pilha e acrescentar cabeçalhos na resposta do CORS (OPTIONS).
app.add_middleware(PrivateNetworkAccessMiddleware)

app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(me_router, prefix="/me", tags=["me"])
app.include_router(v1_router, prefix="/v1")

_api_v1_auth_compat = APIRouter(prefix="/api/v1/auth", tags=["v1-auth"])


@_api_v1_auth_compat.post(
    "/sign-in",
    response_model=FirebaseExchangeResponseV1,
    summary="[Alias] Sign-in email e senha",
    description="Mesmo comportamento que `POST /v1/auth/sign-in` (compatível com URLs tipo `/api/v1/...`).",
    include_in_schema=True,
)
async def post_api_v1_auth_sign_in(
    body: SignInRequestV1,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await firebase_sign_in_with_password_v1(db, body.email, body.password)


app.include_router(_api_v1_auth_compat)


@app.get(
    "/health",
    tags=["Health"],
    summary="Health check",
    description=(
        "Liveness (`status: ok`). Query opcional **`firebase=1`**: inclui objeto `firebase` com "
        "`credential_branch` (path / json / email+PEM / GAC / none), existência de ficheiros e "
        "se o PEM parece válido — **sem** expor chaves ou JSON completo."
    ),
    openapi_extra={
        "responses": {
            "200": {
                "description": "Serviço no ar; com `firebase=1` inclui diagnóstico Firebase Admin.",
                "content": {
                    "application/json": {
                        "examples": {
                            "basic": {"summary": "Só liveness", "value": {"status": "ok"}},
                            "firebase": {
                                "summary": "Com ?firebase=1",
                                "value": {
                                    "status": "ok",
                                    "firebase": {
                                        "firebase_project_id": "parametro-pedagogico",
                                        "api_supports_env_pem": True,
                                        "credential_branch": "FIREBASE_CLIENT_EMAIL_AND_PRIVATE_KEY",
                                        "has_client_email": True,
                                        "has_private_key": True,
                                        "private_key_looks_like_pem": True,
                                    },
                                },
                            },
                        }
                    }
                },
            }
        }
    },
)
async def health(
    firebase: int | None = Query(
        default=None,
        description="Defina **1** para diagnóstico Firebase Admin (sem segredos). Omitir = só `{\"status\":\"ok\"}`.",
    ),
):
    out: dict = {"status": "ok"}
    if firebase == 1:
        out["firebase"] = firebase_credential_diagnostic()
    return out


@app.exception_handler(HTTPException)
async def handle_http_exception(_: Request, exc: HTTPException):
    detail = exc.detail
    message = detail if isinstance(detail, str) else "Request failed"
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.status_code,
                "type": "http_error",
                "message": message,
                "detail": detail,
            }
        },
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_exception(_: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": 422,
                "type": "validation_error",
                "message": "Invalid request payload or params",
                "detail": exc.errors(),
            }
        },
    )


@app.exception_handler(Exception)
async def handle_unexpected_exception(_: Request, exc: Exception):
    msg = str(exc) if settings.api_debug else "Unexpected internal error"
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": {"code": 500, "type": "internal_error", "message": msg}},
    )
