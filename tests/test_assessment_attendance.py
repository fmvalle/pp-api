import uuid

import pytest
from fastapi import HTTPException

from app.core.deps import AuthContext
from app.v1 import assessments_router


def _ctx(role: str) -> AuthContext:
    return AuthContext(
        session_id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        active_profile_id=uuid.uuid4(),
        role=role,
        school_id=uuid.uuid4(),
    )


@pytest.mark.asyncio
async def test_patch_attendance_teacher_forbidden_without_classroom_link(monkeypatch):
    cid = uuid.uuid4()

    async def fake_get_schedule(_sid, _ctx, _db, academic_year_id=None):
        return {"classroom_id": cid, "assessment_id": uuid.uuid4(), "id": _sid}

    async def fake_teacher_manage(_db, _ctx, _classroom_id):
        return False

    monkeypatch.setattr(assessments_router, "get_schedule_v1", fake_get_schedule)
    monkeypatch.setattr(assessments_router, "_schedule_teacher_can_manage", fake_teacher_manage)
    with pytest.raises(HTTPException) as exc:
        await assessments_router.patch_attendance_bulk_v1(
            schedule_id=uuid.uuid4(),
            updates=[],
            ctx=_ctx("teacher"),
            db=_FakeDb(),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_patch_attendance_teacher_ok_when_linked(monkeypatch):
    cid = uuid.uuid4()

    async def fake_get_schedule(_sid, _ctx, _db, academic_year_id=None):
        return {"classroom_id": cid, "assessment_id": uuid.uuid4(), "id": _sid}

    async def fake_teacher_manage(_db, _ctx, classroom_id):
        return str(classroom_id) == str(cid)

    async def fake_apply(_db, _schedule_id, _updates):
        return []

    monkeypatch.setattr(assessments_router, "get_schedule_v1", fake_get_schedule)
    monkeypatch.setattr(assessments_router, "_schedule_teacher_can_manage", fake_teacher_manage)
    monkeypatch.setattr(assessments_router, "_apply_attendance_upserts", fake_apply)
    out = await assessments_router.patch_attendance_bulk_v1(
        schedule_id=uuid.uuid4(),
        updates=[],
        ctx=_ctx("teacher"),
        db=_FakeDb(),
    )
    assert out == []


class _FakeDb:
    async def commit(self) -> None:
        return None
