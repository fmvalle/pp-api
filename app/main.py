from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings
from app.core.firebase import init_firebase
from app.domains.auth.router import router as auth_router
from app.domains.me.router import router as me_router
from app.v1.router import router as v1_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_firebase()
    yield


app = FastAPI(
    title="PP API",
    version="0.1.0",
    lifespan=lifespan,
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


@app.get("/health")
async def health():
    return {"status": "ok"}


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
