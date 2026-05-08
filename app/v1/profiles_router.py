from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext, get_auth_context
from app.db.session import get_db
from app.domains.auth import repository as repo
from app.domains.auth.schemas import ProfileOut

router = APIRouter(prefix="/profiles", tags=["v1-profiles"])


class ProfileMetadataBody(BaseModel):
    metadata: str | None = Field(default=None, description="Persistido em people.metadata para o person_id do perfil.")


@router.get("/{profile_id}", response_model=ProfileOut)
async def get_profile_v1(
    profile_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await repo.get_profile_for_person(db, ctx.person_id, profile_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Profile not found")
    return ProfileOut(
        id=row["id"],
        full_name=row.get("full_name"),
        email=row.get("email"),
        role=str(row["role"]),
        school_id=row.get("school_id"),
        person_id=row.get("person_id"),
        code=row.get("code"),
        metadata=row.get("metadata"),
    )


@router.patch("/{profile_id}/metadata", response_model=ProfileOut)
async def patch_profile_metadata_v1(
    profile_id: UUID,
    body: ProfileMetadataBody,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await repo.get_profile_for_person(db, ctx.person_id, profile_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Profile not found")
    pid = row.get("person_id")
    if not pid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Profile without person_id")
    await repo.update_person_metadata(db, pid, body.metadata)
    await db.commit()
    row2 = await repo.get_profile_for_person(db, ctx.person_id, profile_id)
    if not row2:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Profile disappeared after update")
    return ProfileOut(
        id=row2["id"],
        full_name=row2.get("full_name"),
        email=row2.get("email"),
        role=str(row2["role"]),
        school_id=row2.get("school_id"),
        person_id=row2.get("person_id"),
        code=row2.get("code"),
        metadata=row2.get("metadata"),
    )
