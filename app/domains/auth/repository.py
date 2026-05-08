from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def fetch_person_by_firebase_uid(db: AsyncSession, firebase_uid: str) -> dict | None:
    q = text(
        """
        SELECT id, full_name, email, firebase_uid
        FROM people
        WHERE firebase_uid = :uid
        LIMIT 1
        """
    )
    row = (await db.execute(q, {"uid": firebase_uid})).mappings().first()
    return dict(row) if row else None


async def insert_person(
    db: AsyncSession,
    *,
    firebase_uid: str,
    email: str | None,
    full_name: str | None,
) -> UUID:
    q = text(
        """
        INSERT INTO people (
            id, status, full_name, email, firebase_uid, auth_provider,
            can_login, date_created
        )
        VALUES (
            gen_random_uuid(), 'published', :full_name, :email, :firebase_uid, 'firebase',
            true, now()
        )
        RETURNING id
        """
    )
    row = (
        await db.execute(
            q,
            {"firebase_uid": firebase_uid, "email": email, "full_name": full_name},
        )
    ).one()
    return row[0]


async def update_person_firebase_link(
    db: AsyncSession,
    person_id: UUID,
    *,
    firebase_uid: str,
    email: str | None,
    full_name: str | None,
) -> None:
    q = text(
        """
        UPDATE people
        SET firebase_uid = COALESCE(firebase_uid, :firebase_uid),
            email = COALESCE(email, :email),
            full_name = COALESCE(full_name, :full_name),
            date_updated = now()
        WHERE id = :id
        """
    )
    await db.execute(
        q,
        {
            "id": person_id,
            "firebase_uid": firebase_uid,
            "email": email,
            "full_name": full_name,
        },
    )


async def list_profiles_for_person(db: AsyncSession, person_id: UUID) -> list[dict]:
    q = text(
        """
        SELECT id, full_name, email, role, school_id, person_id, code, metadata
        FROM vw_profiles
        WHERE person_id = :pid
        ORDER BY created_at NULLS LAST
        """
    )
    rows = (await db.execute(q, {"pid": person_id})).mappings().all()
    return [dict(r) for r in rows]


async def get_profile_for_person(db: AsyncSession, person_id: UUID, profile_id: UUID) -> dict | None:
    q = text(
        """
        SELECT id, full_name, email, role, school_id, person_id, code, metadata
        FROM vw_profiles
        WHERE person_id = :pid AND id = :prid
        LIMIT 1
        """
    )
    row = (await db.execute(q, {"pid": person_id, "prid": profile_id})).mappings().first()
    return dict(row) if row else None


async def get_person_row(db: AsyncSession, person_id: UUID) -> dict | None:
    q = text(
        """
        SELECT id, full_name, email, firebase_uid, status, can_login, phone, document, birthdate, metadata
        FROM people
        WHERE id = :id
        LIMIT 1
        """
    )
    row = (await db.execute(q, {"id": person_id})).mappings().first()
    return dict(row) if row else None


async def update_person_metadata(db: AsyncSession, person_id: UUID, metadata: str | None) -> None:
    q = text(
        """
        UPDATE people SET metadata = :metadata, date_updated = now()
        WHERE id = :id
        """
    )
    await db.execute(q, {"id": person_id, "metadata": metadata})


async def get_profile_row_by_id(db: AsyncSession, profile_id: UUID) -> dict | None:
    q = text(
        """
        SELECT id, full_name, email, role, school_id, person_id, code, metadata
        FROM vw_profiles
        WHERE id = :id
        LIMIT 1
        """
    )
    row = (await db.execute(q, {"id": profile_id})).mappings().first()
    return dict(row) if row else None
