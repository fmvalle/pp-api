from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from firebase_admin import auth as firebase_auth
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    create_access_token,
    hash_refresh_token,
    new_refresh_token,
    session_expires_at,
)
from app.domains.auth import repository as repo
from app.domains.auth.models import AppSession
from app.domains.auth.schemas import AuthSessionResponse, ProfileOut, SessionOut, TokenPairResponse


def verify_firebase_id_token(id_token: str) -> dict:
    token = id_token.strip() if id_token else ""
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing Firebase id_token")
    try:
        decoded = firebase_auth.verify_id_token(token, check_revoked=settings.firebase_check_revoked)
        aud = decoded.get("aud")
        iss = decoded.get("iss")
        expected_iss = f"https://securetoken.google.com/{settings.firebase_project_id}"
        if aud != settings.firebase_project_id or iss != expected_iss:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Firebase token com audience/issuer inválidos para este backend.",
            )
        return decoded
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        hint = (
            "Token inválido ou service account de outro projeto. "
            "Use o JSON de 'Contas de serviço' do mesmo Firebase do app (project_id compatível com "
            f"{settings.firebase_project_id}). Ajuste FIREBASE_PROJECT_ID / FIREBASE_CREDENTIALS_PATH."
        )
        detail = f"{hint} ({exc!s})" if settings.api_debug else hint
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail) from exc


async def ensure_person_from_firebase(db: AsyncSession, decoded: dict) -> UUID:
    uid = decoded.get("uid")
    if not uid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token without uid")
    email = decoded.get("email")
    name = decoded.get("name") or email
    existing = await repo.fetch_person_by_firebase_uid(db, uid)
    if existing:
        person_id = existing["id"]
        await repo.update_person_firebase_link(db, person_id, firebase_uid=uid, email=email, full_name=name)
    else:
        person_id = await repo.insert_person(db, firebase_uid=uid, email=email, full_name=name)
    await db.flush()
    return person_id


def _rows_to_profiles(rows: list[dict]) -> list[ProfileOut]:
    return [
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


async def _create_session_and_access(
    db: AsyncSession,
    *,
    person_id: UUID,
    profile_id: UUID,
    profile_row: dict,
    device_info: str | None,
    ip: str | None,
    user_agent: str | None,
) -> tuple[AppSession, str, str]:
    raw_refresh = new_refresh_token()
    expires_at = session_expires_at()
    now = datetime.now(timezone.utc)
    session = AppSession(
        person_id=person_id,
        active_profile_id=profile_id,
        refresh_token_hash=hash_refresh_token(raw_refresh),
        device_info=device_info,
        ip=ip,
        user_agent=user_agent,
        created_at=now,
        last_used_at=now,
        expires_at=expires_at,
        revoked_at=None,
    )
    db.add(session)
    await db.flush()
    access = create_access_token(
        session_id=session.id,
        person_id=person_id,
        profile_id=profile_id,
        role=str(profile_row["role"]),
        school_id=profile_row.get("school_id"),
    )
    return session, raw_refresh, access


async def firebase_exchange(db: AsyncSession, id_token: str) -> tuple[UUID, list[ProfileOut]]:
    decoded = verify_firebase_id_token(id_token)
    person_id = await ensure_person_from_firebase(db, decoded)
    rows = await repo.list_profiles_for_person(db, person_id)
    await db.commit()
    return person_id, _rows_to_profiles(rows)


async def select_profile_for_person(
    db: AsyncSession,
    *,
    person_id: UUID,
    profile_id: UUID,
    device_info: str | None,
    ip: str | None,
    user_agent: str | None,
) -> TokenPairResponse:
    profile_row = await repo.get_profile_for_person(db, person_id, profile_id)
    if not profile_row:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Profile does not belong to this person")

    session, raw_refresh, access = await _create_session_and_access(
        db,
        person_id=person_id,
        profile_id=profile_id,
        profile_row=profile_row,
        device_info=device_info,
        ip=ip,
        user_agent=user_agent,
    )
    await db.commit()
    return TokenPairResponse(
        access_token=access,
        refresh_token=raw_refresh,
        expires_in=settings.access_token_ttl_minutes * 60,
    )


async def select_profile_and_issue_tokens(
    db: AsyncSession,
    *,
    id_token: str,
    profile_id: UUID,
    device_info: str | None,
    ip: str | None,
    user_agent: str | None,
) -> TokenPairResponse:
    decoded = verify_firebase_id_token(id_token)
    person_id = await ensure_person_from_firebase(db, decoded)
    return await select_profile_for_person(
        db,
        person_id=person_id,
        profile_id=profile_id,
        device_info=device_info,
        ip=ip,
        user_agent=user_agent,
    )


async def switch_session_active_profile(
    db: AsyncSession,
    *,
    session_id: UUID,
    person_id: UUID,
    new_profile_id: UUID,
) -> tuple[ProfileOut, str]:
    now = datetime.now(timezone.utc)
    res = await db.execute(
        select(AppSession).where(
            AppSession.id == session_id,
            AppSession.person_id == person_id,
            AppSession.revoked_at.is_(None),
        )
    )
    sess = res.scalar_one_or_none()
    if sess is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session not found")
    if sess.expires_at <= now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")

    profile_row = await repo.get_profile_for_person(db, person_id, new_profile_id)
    if not profile_row:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Profile does not belong to this person")

    sess.active_profile_id = new_profile_id
    sess.last_used_at = now
    access = create_access_token(
        session_id=sess.id,
        person_id=person_id,
        profile_id=new_profile_id,
        role=str(profile_row["role"]),
        school_id=profile_row.get("school_id"),
    )
    await db.commit()

    active = ProfileOut(
        id=profile_row["id"],
        full_name=profile_row.get("full_name"),
        email=profile_row.get("email"),
        role=str(profile_row["role"]),
        school_id=profile_row.get("school_id"),
        person_id=profile_row.get("person_id"),
        code=profile_row.get("code"),
        metadata=profile_row.get("metadata"),
    )
    return active, access


async def create_auth_session(
    db: AsyncSession,
    *,
    id_token: str,
    profile_id: UUID,
    device_info: str | None,
    ip: str | None,
    user_agent: str | None,
) -> AuthSessionResponse:
    decoded = verify_firebase_id_token(id_token)
    person_id = await ensure_person_from_firebase(db, decoded)
    rows = await repo.list_profiles_for_person(db, person_id)
    profiles = _rows_to_profiles(rows)
    profile_row = await repo.get_profile_for_person(db, person_id, profile_id)
    if not profile_row:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Profile does not belong to this person")

    session, raw_refresh, access = await _create_session_and_access(
        db,
        person_id=person_id,
        profile_id=profile_id,
        profile_row=profile_row,
        device_info=device_info,
        ip=ip,
        user_agent=user_agent,
    )
    await db.commit()

    active = ProfileOut(
        id=profile_row["id"],
        full_name=profile_row.get("full_name"),
        email=profile_row.get("email"),
        role=str(profile_row["role"]),
        school_id=profile_row.get("school_id"),
        person_id=profile_row.get("person_id"),
        code=profile_row.get("code"),
        metadata=profile_row.get("metadata"),
    )

    return AuthSessionResponse(
        person_id=person_id,
        profiles=profiles,
        active_profile=active,
        session=SessionOut(id=session.id, expires_at=session.expires_at),
        access_token=access,
        refresh_token=raw_refresh,
        expires_in=settings.access_token_ttl_minutes * 60,
    )


async def refresh_tokens(db: AsyncSession, refresh_token: str) -> TokenPairResponse:
    h = hash_refresh_token(refresh_token)
    now = datetime.now(timezone.utc)
    res = await db.execute(
        select(AppSession).where(
            AppSession.refresh_token_hash == h,
            AppSession.revoked_at.is_(None),
            AppSession.expires_at > now,
        )
    )
    sess = res.scalar_one_or_none()
    if sess is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")

    profile_row = await repo.get_profile_for_person(db, sess.person_id, sess.active_profile_id)
    if not profile_row:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Profile no longer valid")

    raw_refresh = new_refresh_token()
    sess.refresh_token_hash = hash_refresh_token(raw_refresh)
    sess.last_used_at = now
    sess.expires_at = session_expires_at()

    access = create_access_token(
        session_id=sess.id,
        person_id=sess.person_id,
        profile_id=sess.active_profile_id,
        role=str(profile_row["role"]),
        school_id=profile_row.get("school_id"),
    )
    await db.commit()

    return TokenPairResponse(
        access_token=access,
        refresh_token=raw_refresh,
        expires_in=settings.access_token_ttl_minutes * 60,
    )


async def logout_session(db: AsyncSession, refresh_token: str | None, access_session_id: UUID | None) -> None:
    now = datetime.now(timezone.utc)
    if refresh_token:
        h = hash_refresh_token(refresh_token)
        res = await db.execute(
            select(AppSession).where(AppSession.refresh_token_hash == h, AppSession.revoked_at.is_(None))
        )
        sess = res.scalar_one_or_none()
        if sess:
            sess.revoked_at = now
    if access_session_id:
        res = await db.execute(select(AppSession).where(AppSession.id == access_session_id))
        sess = res.scalar_one_or_none()
        if sess and sess.revoked_at is None:
            sess.revoked_at = now
    await db.commit()
