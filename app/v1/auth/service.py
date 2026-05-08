from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import create_bootstrap_token, decode_bootstrap_token
from app.domains.auth import repository as repo
from app.domains.auth.schemas import ProfileOut
from app.domains.auth.service import (
    ensure_person_from_firebase,
    refresh_tokens,
    select_profile_for_person,
    switch_session_active_profile,
    verify_firebase_id_token,
)
from app.v1.auth.schemas import (
    AuthContextResponseV1,
    FirebaseExchangeResponseV1,
    MeResponseV1,
    PatchActiveProfileResponseV1,
    PersonOut,
    SelectProfileResponseV1,
    SessionContextOut,
    TokenPairResponseV1,
)


def _person_out(row: dict) -> PersonOut:
    bd = row.get("birthdate")
    return PersonOut(
        id=row["id"],
        full_name=row.get("full_name"),
        email=row.get("email"),
        status=row.get("status"),
        can_login=row.get("can_login"),
        phone=row.get("phone"),
        document=row.get("document"),
        birthdate=str(bd) if bd is not None else None,
        metadata=row.get("metadata"),
    )


async def firebase_exchange_v1(db: AsyncSession, id_token: str) -> FirebaseExchangeResponseV1:
    decoded = verify_firebase_id_token(id_token)
    person_id = await ensure_person_from_firebase(db, decoded)
    prow = await repo.get_person_row(db, person_id)
    if not prow:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Person row missing after upsert")
    rows = await repo.list_profiles_for_person(db, person_id)
    profiles = [
        ProfileOut(
            id=r["id"],
            full_name=r.get("full_name"),
            email=r.get("email"),
            role=str(r["role"]),
            school_id=r.get("school_id"),
            person_id=r.get("person_id"),
            code=r.get("code"),
            metadata=r.get("metadata"),
        )
        for r in rows
    ]
    await db.commit()
    must = len(profiles) > 1
    bt = create_bootstrap_token(person_id=person_id)
    ctx = SessionContextOut(person_id=person_id, active_profile_id=None, role=None, school_id=None)
    return FirebaseExchangeResponseV1(
        person=_person_out(prow),
        profiles=profiles,
        must_select_profile=must,
        bootstrap_token=bt,
        context=ctx,
    )


async def select_profile_v1(
    db: AsyncSession,
    *,
    profile_id: UUID,
    bootstrap_token: str | None,
    id_token: str | None,
    device_info: str | None,
    ip: str | None,
    user_agent: str | None,
) -> SelectProfileResponseV1:
    person_id: UUID
    if bootstrap_token:
        try:
            person_id = decode_bootstrap_token(bootstrap_token)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired bootstrap_token") from exc
    elif id_token:
        decoded = verify_firebase_id_token(id_token)
        person_id = await ensure_person_from_firebase(db, decoded)
    else:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Informe bootstrap_token (recomendado) ou id_token.",
        )

    pair = await select_profile_for_person(
        db,
        person_id=person_id,
        profile_id=profile_id,
        device_info=device_info,
        ip=ip,
        user_agent=user_agent,
    )
    prof_row = await repo.get_profile_for_person(db, person_id, profile_id)
    if not prof_row:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Profile missing after session create")
    active = ProfileOut(
        id=prof_row["id"],
        full_name=prof_row.get("full_name"),
        email=prof_row.get("email"),
        role=str(prof_row["role"]),
        school_id=prof_row.get("school_id"),
        person_id=prof_row.get("person_id"),
        code=prof_row.get("code"),
        metadata=prof_row.get("metadata"),
    )
    ctx = AuthContextResponseV1(
        person_id=person_id,
        active_profile_id=profile_id,
        role=str(prof_row["role"]),
        school_id=prof_row.get("school_id"),
    )
    return SelectProfileResponseV1(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
        active_profile=active,
        context=ctx,
    )


async def refresh_v1(db: AsyncSession, refresh_token: str) -> TokenPairResponseV1:
    p = await refresh_tokens(db, refresh_token)
    return TokenPairResponseV1(
        access_token=p.access_token,
        refresh_token=p.refresh_token,
        expires_in=p.expires_in,
    )


async def me_v1(db: AsyncSession, person_id: UUID, active_profile_id: UUID) -> MeResponseV1:
    person = await repo.get_person_row(db, person_id)
    if not person:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Person not found")
    prof = await repo.get_profile_for_person(db, person_id, active_profile_id)
    if not prof:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Active profile not found")
    active = ProfileOut(
        id=prof["id"],
        full_name=prof.get("full_name"),
        email=prof.get("email"),
        role=str(prof["role"]),
        school_id=prof.get("school_id"),
        person_id=prof.get("person_id"),
        code=prof.get("code"),
        metadata=prof.get("metadata"),
    )
    ctx = SessionContextOut(
        person_id=person_id,
        active_profile_id=active_profile_id,
        role=str(prof["role"]),
        school_id=prof.get("school_id"),
    )
    return MeResponseV1(
        person=_person_out(person),
        active_profile=active,
        role=str(prof["role"]),
        school_id=prof.get("school_id"),
        context=ctx,
    )


async def apply_active_profile_switch(
    db: AsyncSession,
    *,
    session_id: UUID,
    person_id: UUID,
    new_profile_id: UUID,
) -> PatchActiveProfileResponseV1:
    active, access = await switch_session_active_profile(
        db,
        session_id=session_id,
        person_id=person_id,
        new_profile_id=new_profile_id,
    )
    ctx = AuthContextResponseV1(
        person_id=person_id,
        active_profile_id=active.id,
        role=active.role,
        school_id=active.school_id,
    )
    return PatchActiveProfileResponseV1(
        access_token=access,
        expires_in=settings.access_token_ttl_minutes * 60,
        active_profile=active,
        context=ctx,
    )
