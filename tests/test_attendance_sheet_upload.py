import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import ProgrammingError

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
async def test_upload_attendance_sheet_teacher_forbidden_without_classroom_link(monkeypatch):
    cid = uuid.uuid4()

    async def fake_get_schedule(_sid, _ctx, _db, academic_year_id=None):
        return {"classroom_id": cid, "assessment_id": uuid.uuid4(), "id": _sid}

    async def fake_teacher_manage(_db, _ctx, _classroom_id):
        return False

    monkeypatch.setattr(assessments_router, "get_schedule_v1", fake_get_schedule)
    monkeypatch.setattr(assessments_router, "_schedule_teacher_can_manage", fake_teacher_manage)

    class _F:
        async def read(self):
            return b"%PDF-1.4 test"

    class _UF:
        content_type = "application/pdf"
        filename = "x.pdf"

        async def read(self):
            return b"%PDF-1.4 test"

    with pytest.raises(HTTPException) as exc:
        await assessments_router.upload_schedule_attendance_sheet_v1(
            schedule_id=uuid.uuid4(),
            ctx=_ctx("teacher"),
            db=object(),
            file=_UF(),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_pdf_attendance_sheet_teacher_ok_when_linked(monkeypatch):
    cid = uuid.uuid4()
    sid = uuid.uuid4()

    async def fake_get_schedule(_sid, _ctx, _db, academic_year_id=None):
        return {
            "classroom_id": cid,
            "assessment_id": uuid.uuid4(),
            "id": sid,
            "start_time": "2026-01-01T10:00:00+00:00",
            "end_time": "2026-01-01T12:00:00+00:00",
        }

    async def fake_teacher_manage(_db, _ctx, classroom_id):
        return str(classroom_id) == str(cid)

    async def fake_roster(_sid, _ctx, _db, academic_year_id=None):
        return {
            "schedule": await fake_get_schedule(_sid, _ctx, _db, academic_year_id),
            "classroom": {"name": "3A", "code": "C1", "academic_year_id": uuid.uuid4()},
            "students": [{"full_name": "Aluno Um", "code": "123", "metadata": {}}],
        }

    async def fake_assessment_row(_db, _sql, _params):
        return {"title": "Prova", "school_name": "Escola X"}

    async def fake_ay(_db, _sql, _params):
        return {"year": 2026}

    monkeypatch.setattr(assessments_router, "get_schedule_v1", fake_get_schedule)
    monkeypatch.setattr(assessments_router, "_schedule_teacher_can_manage", fake_teacher_manage)
    monkeypatch.setattr(assessments_router, "get_schedule_roster_v1", fake_roster)

    call = {"n": 0}

    async def fetch_one(db, sql, params):
        call["n"] += 1
        if "assessments a" in sql:
            return await fake_assessment_row(db, sql, params)
        if "academic_years" in sql:
            return await fake_ay(db, sql, params)
        return None

    monkeypatch.setattr(assessments_router, "fetch_one", fetch_one)

    resp = await assessments_router.download_schedule_attendance_sheet_pdf_v1(
        schedule_id=sid,
        ctx=_ctx("teacher"),
        db=object(),
    )
    assert resp.media_type == "application/pdf"
    assert len(resp.body) > 100
    assert resp.body[:4] == b"%PDF"


@pytest.mark.asyncio
async def test_list_attendance_sheet_versions(monkeypatch):
    sid = uuid.uuid4()
    vid = uuid.uuid4()

    async def fake_get_schedule(_sid, _ctx, _db, academic_year_id=None):
        return {"classroom_id": uuid.uuid4(), "assessment_id": uuid.uuid4(), "id": sid}

    async def fake_assert_editor(_db, _ctx, _sch):
        return None

    async def fake_fetch_all(_db, _sql, _params):
        return [
            {
                "id": vid,
                "original_filename": "lista.pdf",
                "content_type": "application/pdf",
                "size_bytes": 12,
                "uploaded_by": uuid.uuid4(),
                "created_at": "2026-01-01T12:00:00+00:00",
            }
        ]

    monkeypatch.setattr(assessments_router, "get_schedule_v1", fake_get_schedule)
    monkeypatch.setattr(assessments_router, "_assert_schedule_attendance_editor", fake_assert_editor)
    monkeypatch.setattr(assessments_router, "fetch_all", fake_fetch_all)

    out = await assessments_router.list_schedule_attendance_sheet_versions_v1(
        schedule_id=sid,
        ctx=_ctx("teacher"),
        db=object(),
    )
    assert out["items"][0]["id"] == vid
    assert out["items"][0]["original_filename"] == "lista.pdf"


@pytest.mark.asyncio
async def test_download_attendance_sheet_upload_latest(monkeypatch):
    sid = uuid.uuid4()

    async def fake_get_schedule(_sid, _ctx, _db, academic_year_id=None):
        return {"classroom_id": uuid.uuid4(), "assessment_id": uuid.uuid4(), "id": sid}

    async def fake_assert_editor(_db, _ctx, _sch):
        return None

    async def fake_fetch_one(_db, sql, _params):
        if "ORDER BY created_at DESC" in sql:
            return {
                "storage_key": "ab.upload",
                "original_filename": "x.pdf",
                "content_type": "application/pdf",
            }
        return None

    monkeypatch.setattr(assessments_router, "get_schedule_v1", fake_get_schedule)
    monkeypatch.setattr(assessments_router, "_assert_schedule_attendance_editor", fake_assert_editor)
    monkeypatch.setattr(assessments_router, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(assessments_router, "load_attendance_sheet_bytes", lambda _k: b"%PDF-1.4")

    resp = await assessments_router.download_schedule_attendance_sheet_upload_v1(
        schedule_id=sid,
        ctx=_ctx("teacher"),
        db=object(),
        attendance_sheet_id=None,
    )
    assert resp.media_type == "application/pdf"
    assert resp.body.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_upload_accepts_octet_stream_pdf_by_signature(monkeypatch):
    sid = uuid.uuid4()
    class _DB:
        async def commit(self):
            return None

    async def fake_get_schedule(_sid, _ctx, _db, academic_year_id=None):
        return {"classroom_id": uuid.uuid4(), "assessment_id": uuid.uuid4(), "id": sid}

    async def fake_assert_editor(_db, _ctx, _sch):
        return None

    async def fake_fetch_one(_db, _sql, params):
        return {"id": uuid.uuid4(), "content_type": params["ct"]}

    monkeypatch.setattr(assessments_router, "get_schedule_v1", fake_get_schedule)
    monkeypatch.setattr(assessments_router, "_assert_schedule_attendance_editor", fake_assert_editor)
    monkeypatch.setattr(assessments_router, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(assessments_router, "store_attendance_sheet_bytes", lambda _k, _b: None)

    class _UF:
        content_type = "application/octet-stream"
        filename = "lista-gerada.pdf"

        async def read(self):
            return b"%PDF-1.4 generated"

    row = await assessments_router.upload_schedule_attendance_sheet_v1(
        schedule_id=sid,
        ctx=_ctx("school_admin"),
        db=_DB(),
        file=_UF(),
    )
    assert row["content_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_versions_returns_empty_when_table_missing(monkeypatch):
    sid = uuid.uuid4()

    async def fake_get_schedule(_sid, _ctx, _db, academic_year_id=None):
        return {"classroom_id": uuid.uuid4(), "assessment_id": uuid.uuid4(), "id": sid}

    async def fake_assert_editor(_db, _ctx, _sch):
        return None

    async def fake_fetch_all(_db, _sql, _params):
        raise ProgrammingError(
            statement="SELECT * FROM assessment_schedule_attendance_sheet",
            params={},
            orig=Exception('relation "assessment_schedule_attendance_sheet" does not exist'),
        )

    monkeypatch.setattr(assessments_router, "get_schedule_v1", fake_get_schedule)
    monkeypatch.setattr(assessments_router, "_assert_schedule_attendance_editor", fake_assert_editor)
    monkeypatch.setattr(assessments_router, "fetch_all", fake_fetch_all)

    out = await assessments_router.list_schedule_attendance_sheet_versions_v1(
        schedule_id=sid,
        ctx=_ctx("school_admin"),
        db=object(),
    )
    assert out == {"items": []}
