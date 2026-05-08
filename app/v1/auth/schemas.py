from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.domains.auth.schemas import ProfileOut


class PersonOut(BaseModel):
    id: UUID
    full_name: str | None = None
    email: str | None = None
    status: str | None = None
    can_login: bool | None = None
    phone: str | None = None
    document: str | None = None
    birthdate: str | None = None
    metadata: dict[str, Any] | list[Any] | str | None = None


class SessionContextOut(BaseModel):
    person_id: UUID
    active_profile_id: UUID | None = None
    role: str | None = None
    school_id: UUID | None = None


class FirebaseExchangeRequestV1(BaseModel):
    id_token: str = Field(..., min_length=10)

    @model_validator(mode="before")
    @classmethod
    def accept_camel_case_payload(cls, data: object) -> object:
        # Backward compatibility for cached frontends still sending `idToken`.
        if isinstance(data, dict) and "id_token" not in data and "idToken" in data:
            data = dict(data)
            data["id_token"] = data.get("idToken")
        return data

    @field_validator("id_token", mode="before")
    @classmethod
    def strip_token(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v


class FirebaseExchangeResponseV1(BaseModel):
    person: PersonOut
    profiles: list[ProfileOut]
    must_select_profile: bool
    bootstrap_token: str
    context: SessionContextOut


class SelectProfileRequestV1(BaseModel):
    profile_id: UUID
    bootstrap_token: str | None = Field(default=None, description="Preferencial após exchange.")
    id_token: str | None = Field(default=None, description="Alternativa legada ao bootstrap_token.")

    @model_validator(mode="before")
    @classmethod
    def accept_camel_case_payload(cls, data: object) -> object:
        if isinstance(data, dict) and "id_token" not in data and "idToken" in data:
            data = dict(data)
            data["id_token"] = data.get("idToken")
        return data

    @field_validator("bootstrap_token", "id_token", mode="before")
    @classmethod
    def strip_opt(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip() or None
        return v


class AuthContextResponseV1(BaseModel):
    person_id: UUID
    active_profile_id: UUID
    role: str
    school_id: UUID | None = None


class SelectProfileResponseV1(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    active_profile: ProfileOut
    context: AuthContextResponseV1


class RefreshRequestV1(BaseModel):
    refresh_token: str = Field(..., min_length=10)


class TokenPairResponseV1(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class LogoutRequestV1(BaseModel):
    refresh_token: str | None = None


class PatchActiveProfileRequestV1(BaseModel):
    profile_id: UUID


class PatchActiveProfileResponseV1(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    active_profile: ProfileOut
    context: AuthContextResponseV1


class MeResponseV1(BaseModel):
    person: PersonOut
    active_profile: ProfileOut
    role: str
    school_id: UUID | None = None
    context: SessionContextOut


class ProfileMetadataPatchV1(BaseModel):
    metadata: dict[str, Any] | list[Any] | str | None = None
