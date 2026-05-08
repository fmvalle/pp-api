from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class FirebaseExchangeRequest(BaseModel):
    id_token: str = Field(..., min_length=10)

    @model_validator(mode="before")
    @classmethod
    def accept_camel_case_payload(cls, data: object) -> object:
        if isinstance(data, dict) and "id_token" not in data and "idToken" in data:
            data = dict(data)
            data["id_token"] = data.get("idToken")
        return data

    @field_validator("id_token", mode="before")
    @classmethod
    def strip_id_token(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v


class ProfileOut(BaseModel):
    id: UUID
    full_name: str | None = None
    email: str | None = None
    role: str
    school_id: UUID | None = None
    person_id: UUID | None = None
    code: str | None = None
    metadata: dict[str, Any] | list[Any] | str | None = None


class FirebaseExchangeResponse(BaseModel):
    person_id: UUID
    profiles: list[ProfileOut]


class SelectProfileRequest(BaseModel):
    profile_id: UUID
    id_token: str = Field(
        ...,
        min_length=10,
        description="Firebase ID token para provar identidade antes de existir access token da API.",
    )

    @model_validator(mode="before")
    @classmethod
    def accept_camel_case_payload(cls, data: object) -> object:
        if isinstance(data, dict) and "id_token" not in data and "idToken" in data:
            data = dict(data)
            data["id_token"] = data.get("idToken")
        return data

    @field_validator("id_token", mode="before")
    @classmethod
    def strip_id_token_select(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v


class AuthSessionRequest(BaseModel):
    """Um único passo: Firebase + perfil ativo + criação de `app_sessions` e tokens."""

    id_token: str = Field(..., min_length=10)
    profile_id: UUID

    @model_validator(mode="before")
    @classmethod
    def accept_camel_case_payload(cls, data: object) -> object:
        if isinstance(data, dict) and "id_token" not in data and "idToken" in data:
            data = dict(data)
            data["id_token"] = data.get("idToken")
        return data

    @field_validator("id_token", mode="before")
    @classmethod
    def strip_id_token_session(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v


class SessionOut(BaseModel):
    id: UUID
    expires_at: datetime


class AuthSessionResponse(BaseModel):
    person_id: UUID
    profiles: list[ProfileOut]
    active_profile: ProfileOut
    session: SessionOut
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=10)


class LogoutRequest(BaseModel):
    refresh_token: str | None = Field(default=None)
