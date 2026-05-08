from dataclasses import dataclass
from uuid import UUID

from fastapi import Query


@dataclass
class PageArgs:
    page: int
    per_page: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page


def pagination_params(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
) -> PageArgs:
    return PageArgs(page=page, per_page=per_page)


def paged_response(*, page: int, per_page: int, total: int, items: list):
    return {
        "page": page,
        "per_page": per_page,
        "total": total,
        "items": items,
    }


def paged_response_with_academic_year(
    *,
    academic_year_id: UUID,
    page: int,
    per_page: int,
    total: int,
    items: list,
):
    """Mesmo envelope de [paged_response] + `academic_year_id` resolvido (UUID string)."""
    out = paged_response(page=page, per_page=per_page, total=total, items=items)
    out["academic_year_id"] = str(academic_year_id)
    return out

