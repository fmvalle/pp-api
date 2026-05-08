from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.v1._sql import fetch_one


async def resolve_academic_year_id(db: AsyncSession, requested_id: UUID | None) -> UUID:
    """Resolve o ano letivo efetivo:
    - se informado, valida existência
    - senão, usa o registro com is_primary=true
    """
    if requested_id is not None:
        row = await fetch_one(
            db,
            "SELECT id FROM academic_years WHERE id = CAST(:id AS uuid) LIMIT 1",
            {"id": str(requested_id)},
        )
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "academic_year_id não encontrado")
        return row["id"]

    primary = await fetch_one(
        db,
        "SELECT id FROM academic_years WHERE is_primary = true LIMIT 1",
        {},
    )
    if not primary:
        raise HTTPException(status.HTTP_409_CONFLICT, "Nenhum academic_year primário (is_primary=true) configurado")
    return primary["id"]

