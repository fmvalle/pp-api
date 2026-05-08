from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext, get_auth_context
from app.db.session import get_db
from app.domains.auth import repository as repo
from app.domains.auth.schemas import ProfileOut

router = APIRouter()


class MePersonOut(BaseModel):
    id: str
    full_name: str | None = None
    email: str | None = None
    status: str | None = None
    can_login: bool | None = None


class MeOut(BaseModel):
    person: MePersonOut
    active_profile: ProfileOut
    role: str
    school_id: str | None


@router.get("", response_model=MeOut)
async def get_me(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    person = await repo.get_person_row(db, ctx.person_id)
    if not person:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Person not found")

    prof = await repo.get_profile_for_person(db, ctx.person_id, ctx.active_profile_id)
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

    return MeOut(
        person=MePersonOut(
            id=str(person["id"]),
            full_name=person.get("full_name"),
            email=person.get("email"),
            status=person.get("status"),
            can_login=person.get("can_login"),
        ),
        active_profile=active,
        role=ctx.role,
        school_id=str(ctx.school_id) if ctx.school_id else None,
    )


@router.get("/profiles", response_model=list[ProfileOut])
async def get_me_profiles(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    rows = await repo.list_profiles_for_person(db, ctx.person_id)
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


@router.get("/active-profile", response_model=ProfileOut)
async def get_me_active_profile(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    prof = await repo.get_profile_for_person(db, ctx.person_id, ctx.active_profile_id)
    if not prof:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Active profile not found")

    return ProfileOut(
        id=prof["id"],
        full_name=prof.get("full_name"),
        email=prof.get("email"),
        role=str(prof["role"]),
        school_id=prof.get("school_id"),
        person_id=prof.get("person_id"),
        code=prof.get("code"),
        metadata=prof.get("metadata"),
    )
