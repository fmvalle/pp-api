from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext, get_auth_context
from app.db.session import get_db
from app.domains.auth import repository as repo
from app.domains.auth.schemas import ProfileOut
from app.v1.auth.schemas import MeResponseV1, PatchActiveProfileRequestV1, PatchActiveProfileResponseV1
from app.v1.auth.service import apply_active_profile_switch, me_v1

router = APIRouter()


@router.get("", response_model=MeResponseV1)
async def get_me_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await me_v1(db, ctx.person_id, ctx.active_profile_id)


@router.get("/profiles", response_model=list[ProfileOut])
async def get_me_profiles_v1(
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
async def get_me_active_profile_v1(
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


@router.patch("/active-profile", response_model=PatchActiveProfileResponseV1)
async def patch_me_active_profile_v1(
    body: PatchActiveProfileRequestV1,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await apply_active_profile_switch(
        db,
        session_id=ctx.session_id,
        person_id=ctx.person_id,
        new_profile_id=body.profile_id,
    )
