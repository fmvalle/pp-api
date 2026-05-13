from functools import cached_property
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Quando CORS_ORIGINS está vazio, lista explícita usada junto ao regex de localhost (qualquer porta).
_DEV_CORS_FALLBACK: tuple[str, ...] = (
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
)

# Navegadores nunca enviam Origin com "*"; CORS não aceita wildcard de porta na lista. Usar allow_origin_regex.
# Inclui [::1] — alguns stacks Flutter/web usam IPv6 literal no Origin.
_LOCALHOST_ANY_PORT_ORIGIN_REGEX: str = (
    r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(
        ...,
        description="Async URL, ex: postgresql+asyncpg://postgres:postgres@db:5432/pp_db",
    )

    jwt_secret: str = Field(..., min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    bootstrap_token_ttl_minutes: int = 10
    refresh_token_ttl_days: int = 30

    refresh_token_pepper: str = Field(..., min_length=16)

    firebase_credentials_json: str | None = Field(
        default=None,
        description="JSON completo da service account (preferir arquivo montado em produção)",
    )
    firebase_credentials_path: str | None = Field(
        default=None,
        description="Caminho para JSON da service account",
    )
    firebase_project_id: str = Field(
        default="parametro-pedagogico",
        description="Project ID do Firebase que emite os ID tokens (deve bater com o app cliente).",
    )
    firebase_client_email: str | None = Field(
        default=None,
        description="Email da service account (ex.: firebase-adminsdk-…@proj.iam.gserviceaccount.com); "
        "com FIREBASE_PRIVATE_KEY substitui JSON/path (estilo Directus / Cloud Run).",
    )
    firebase_private_key: str | None = Field(
        default=None,
        description="PEM RSA da service account; use \\n literais se a env for uma linha só.",
    )
    firebase_web_api_key: str | None = Field(
        default=None,
        description="Chave Web do Firebase (Identity Toolkit) para POST /v1/auth/sign-in com email+senha.",
    )
    firebase_check_revoked: bool = Field(
        default=False,
        description="Se true, verify_id_token consulta revogação (mais lento; pode falhar sem permissões).",
    )
    api_debug: bool = Field(
        default=False,
        description="Se true, erros de auth podem incluir detalhe da exceção (apenas desenvolvimento).",
    )

    cors_origins: str = Field(
        default="",
        description="Lista CSV de origens exatas (scheme+host+porta). Entradas com '*' são ignoradas.",
    )
    cors_allow_localhost_any_port: bool = Field(
        default=True,
        description="Se true, permite Origin http(s)://localhost e 127.0.0.1 com qualquer porta (regex Starlette), além da lista.",
    )
    cors_origin_regex: str = Field(
        default="",
        description="Regex Python para Origin; se definido, substitui o regex automático de localhost (inclua localhost aí se precisar).",
    )

    attendance_sheet_storage_path: str = Field(
        default="var/attendance_sheets",
        description="Diretório (relativo ao cwd da API) quando attendance_sheet_storage_backend=local.",
    )

    attendance_sheet_storage_backend: Literal["local", "gcs"] = Field(
        default="local",
        description="local = disco (ATTENDANCE_SHEET_STORAGE_PATH); gcs = Google Cloud Storage.",
    )

    gcs_attendance_sheets_bucket: str | None = Field(
        default=None,
        description="Bucket GCS para uploads de lista de presença (obrigatório se backend=gcs).",
    )

    gcs_attendance_sheets_prefix: str = Field(
        default="attendance-sheets",
        description="Prefixo dos objetos no bucket (sem barras inicial/final).",
    )

    gcs_credentials_json: str | None = Field(
        default=None,
        description="JSON completo da service account GCS (opcional; senão use GOOGLE_APPLICATION_CREDENTIALS).",
    )

    attendance_sheet_max_bytes: int = Field(
        default=20_971_520,
        description="Tamanho máximo do upload (20 MiB por defeito).",
    )

    answer_card_template_path: str | None = Field(
        default=None,
        description="Caminho absoluto ou relativo ao cwd para Cartao_Resposta-modelo.pdf. "
        "Se vazio, usa app/v1/assets/Cartao_Resposta-modelo.pdf quando existir.",
    )

    @model_validator(mode="after")
    def _validate_attendance_storage(self):
        if self.attendance_sheet_storage_backend == "gcs":
            if not (self.gcs_attendance_sheets_bucket or "").strip():
                raise ValueError(
                    "gcs_attendance_sheets_bucket é obrigatório quando attendance_sheet_storage_backend=gcs"
                )
        return self

    @cached_property
    def cors_origins_list(self) -> list[str]:
        raw: list[str] = []
        for o in self.cors_origins.split(","):
            o = o.strip().rstrip("/")
            if not o or "*" in o:
                continue
            raw.append(o)
        if raw:
            return raw
        return list(_DEV_CORS_FALLBACK)

    @cached_property
    def cors_allow_origin_regex(self) -> str | None:
        custom = (self.cors_origin_regex or "").strip()
        if custom:
            return custom
        if self.cors_allow_localhost_any_port:
            return _LOCALHOST_ANY_PORT_ORIGIN_REGEX
        return None


settings = Settings()
