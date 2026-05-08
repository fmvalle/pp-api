import uuid

import pytest

from app.v1 import directory_router


@pytest.mark.asyncio
async def test_create_user_with_profile_happy_path(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch_one(_db, sql, _params):
        if "RETURNING id" in sql and "INSERT INTO people" in sql:
            return {"id": "p1"}
        if "INSERT INTO profiles" in sql:
            return {"id": "pr1"}
        if "FROM vw_profiles" in sql:
            return {"id": "pr1", "role": "student", "person_id": "p1", "school_id": None}
        return {"ok": True}

    async def fake_execute(_db, _sql, _params):
        calls["n"] += 1

    monkeypatch.setattr(directory_router, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(directory_router, "execute", fake_execute)

    class DummyDB:
        async def commit(self):
            return None

    body = directory_router.UserCreateBody(
        full_name="A",
        email="a@a.com",
        role="student",
        create_firebase_user=False,
    )
    out = await directory_router._create_user_with_profile(DummyDB(), body=body)
    assert out["id"] == "pr1"
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_users_import_collects_errors(monkeypatch):
    async def fake_create(_db, body):
        if body.email == "bad@example.com":
            raise RuntimeError("boom")
        return {"id": str(uuid.uuid4()), "email": body.email}

    monkeypatch.setattr(directory_router, "_create_user_with_profile", fake_create)
    async def fake_execute(_db, _sql, _params):
        return None

    async def fake_fetch_one(_db, _sql, _params):
        return None

    monkeypatch.setattr(directory_router, "execute", fake_execute)
    monkeypatch.setattr(directory_router, "fetch_one", fake_fetch_one)
    ctx = directory_router.AuthContext(  # type: ignore[attr-defined]
        session_id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        active_profile_id=uuid.uuid4(),
        role="admin",
        school_id=None,
    )
    users = [
        directory_router.UserCreateBody(full_name="a", email="ok@example.com", role="teacher", create_firebase_user=False),
        directory_router.UserCreateBody(full_name="b", email="bad@example.com", role="teacher", create_firebase_user=False),
    ]
    class DummyDB:
        async def commit(self):
            return None

        async def rollback(self):
            return None

    payload = directory_router.ImportUsersRequest(users=users, continue_on_error=True)
    out = await directory_router.import_users_v1(payload=payload, ctx=ctx, db=DummyDB())  # type: ignore[arg-type]
    assert out["ok"] is False
    assert len(out["created"]) == 1
    assert len(out["errors"]) == 1


@pytest.mark.asyncio
async def test_users_import_accepts_csv_rows(monkeypatch):
    async def fake_create(_db, body):
        return {"id": str(uuid.uuid4()), "email": body.email}

    monkeypatch.setattr(directory_router, "_create_user_with_profile", fake_create)

    async def fake_link(*_a, **_k):
        return None

    monkeypatch.setattr(directory_router, "_link_import_profile_to_classroom", fake_link)

    async def fake_execute(_db, _sql, _params):
        return None

    async def fake_fetch_one(_db, _sql, _params):
        if "FROM app_import_jobs" in _sql or "app_import_jobs" in _sql:
            return None
        if "people WHERE lower(email)" in _sql:
            return None
        return None

    monkeypatch.setattr(directory_router, "execute", fake_execute)
    monkeypatch.setattr(directory_router, "fetch_one", fake_fetch_one)
    ctx = directory_router.AuthContext(  # type: ignore[attr-defined]
        session_id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        active_profile_id=uuid.uuid4(),
        role="platform_admin",
        school_id=None,
    )

    class DummyDB:
        async def commit(self):
            return None

        async def rollback(self):
            return None

    sid = str(uuid.uuid4())
    cid = str(uuid.uuid4())
    payload = directory_router.ImportUsersRequest(
        rows=[
            {
                "full_name": "Aluno CSV",
                "email": "csv.student@example.com",
                "role": "STUDENT",
                "school_id": sid,
                "classroom_id": cid,
            }
        ],
        continue_on_error=True,
    )
    out = await directory_router.import_users_v1(payload=payload, ctx=ctx, db=DummyDB())  # type: ignore[arg-type]
    assert out["ok"] is True
    assert out["summary"]["received"] == 1
    assert len(out["created"]) == 1
    assert out["created"][0].get("classroom_linked") is True
