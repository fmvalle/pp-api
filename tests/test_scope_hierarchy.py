import pytest
from fastapi import HTTPException
from uuid import UUID

from app.core.deps import AuthContext
from app.v1 import _scope


def test_is_staff_admin_role_accepts_app_jwt_roles():
    assert _scope.is_staff_admin_role("school_admin")
    assert _scope.is_staff_admin_role("SCHOOL_ADMIN")
    assert _scope.is_staff_admin_role("platform-admin")
    assert _scope.is_staff_admin_role("admin")
    assert not _scope.is_staff_admin_role("teacher")
    assert not _scope.is_staff_admin_role("student")


@pytest.mark.asyncio
async def test_resolve_admin_dashboard_platform_admin_global(monkeypatch):
    async def fake_desc(_db, sid):
        return [sid]

    monkeypatch.setattr(_scope, "get_descendant_school_ids", fake_desc)
    ctx = AuthContext(
        session_id="00000000-0000-0000-0000-000000000010",  # type: ignore[arg-type]
        person_id="00000000-0000-0000-0000-000000000011",  # type: ignore[arg-type]
        active_profile_id="00000000-0000-0000-0000-000000000012",  # type: ignore[arg-type]
        role="platform_admin",
        school_id=None,
    )
    assert await _scope.resolve_admin_dashboard_school_ids(object(), ctx, None) is None
    sid = UUID("00000000-0000-0000-0000-000000000099")
    assert await _scope.resolve_admin_dashboard_school_ids(object(), ctx, sid) == [sid]


@pytest.mark.asyncio
async def test_resolve_admin_dashboard_school_admin_rejects_foreign_school(monkeypatch):
    root = UUID("00000000-0000-0000-0000-000000000001")
    child = UUID("00000000-0000-0000-0000-000000000002")

    async def fake_effective(_db, _ctx):
        return {
            "effective_school_ids": [root, child],
            "direct_school_id": root,
            "is_admin_like": False,
        }

    monkeypatch.setattr(_scope, "get_effective_school_scope", fake_effective)

    async def fake_desc(_db, sid):
        return [sid, child] if sid == root else [sid]

    monkeypatch.setattr(_scope, "get_descendant_school_ids", fake_desc)
    ctx = AuthContext(
        session_id="00000000-0000-0000-0000-000000000010",  # type: ignore[arg-type]
        person_id="00000000-0000-0000-0000-000000000011",  # type: ignore[arg-type]
        active_profile_id="00000000-0000-0000-0000-000000000012",  # type: ignore[arg-type]
        role="school_admin",
        school_id=root,  # type: ignore[arg-type]
    )
    out = await _scope.resolve_admin_dashboard_school_ids(object(), ctx, None)
    assert root in out and child in out
    foreign = UUID("00000000-0000-0000-0000-00000000dead")
    with pytest.raises(HTTPException) as exc:
        await _scope.resolve_admin_dashboard_school_ids(object(), ctx, foreign)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_effective_school_scope_always_includes_profile_school_id(monkeypatch):
    """Mesmo se o CTE retornasse só filhos (regressão), o nó do perfil permanece no conjunto."""

    async def fake_fetch_all(_db, sql, params):
        if "WITH RECURSIVE descendants" in sql:
            return [{"id": "00000000-0000-0000-0000-000000000002"}]
        return []

    monkeypatch.setattr(_scope, "fetch_all", fake_fetch_all)
    ctx = AuthContext(
        session_id="00000000-0000-0000-0000-000000000010",  # type: ignore[arg-type]
        person_id="00000000-0000-0000-0000-000000000011",  # type: ignore[arg-type]
        active_profile_id="00000000-0000-0000-0000-000000000012",  # type: ignore[arg-type]
        role="school_admin",
        school_id="00000000-0000-0000-0000-000000000001",  # type: ignore[arg-type]
    )
    scope = await _scope.get_effective_school_scope(object(), ctx)
    ids = [str(x) for x in scope["effective_school_ids"]]
    assert "00000000-0000-0000-0000-000000000001" in ids
    assert "00000000-0000-0000-0000-000000000002" in ids


@pytest.mark.asyncio
async def test_effective_school_scope_includes_descendants(monkeypatch):
    async def fake_fetch_all(_db, sql, params):
        if "WITH RECURSIVE descendants" in sql:
            assert params["sid"] == "00000000-0000-0000-0000-000000000001"
            return [
                {"id": "00000000-0000-0000-0000-000000000001"},
                {"id": "00000000-0000-0000-0000-000000000002"},
                {"id": "00000000-0000-0000-0000-000000000003"},
            ]
        return []

    monkeypatch.setattr(_scope, "fetch_all", fake_fetch_all)
    ctx = AuthContext(
        session_id="00000000-0000-0000-0000-000000000010",  # type: ignore[arg-type]
        person_id="00000000-0000-0000-0000-000000000011",  # type: ignore[arg-type]
        active_profile_id="00000000-0000-0000-0000-000000000012",  # type: ignore[arg-type]
        role="school_admin",
        school_id="00000000-0000-0000-0000-000000000001",  # type: ignore[arg-type]
    )
    scope = await _scope.get_effective_school_scope(object(), ctx)
    assert len(scope["effective_school_ids"]) == 3


@pytest.mark.asyncio
async def test_effective_classroom_scope_union_school_and_explicit(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch_all(_db, sql, _params):
        if "WITH RECURSIVE descendants" in sql:
            return [{"id": "s1"}, {"id": "s2"}]
        if "FROM classroom_teachers" in sql:
            return [{"classroom_id": "c3"}]
        if "FROM classrooms" in sql:
            calls["n"] += 1
            return [{"classroom_id": "c1"}, {"classroom_id": "c2"}, {"classroom_id": "c3"}]
        return []

    monkeypatch.setattr(_scope, "fetch_all", fake_fetch_all)
    ctx = AuthContext(
        session_id="x",  # type: ignore[arg-type]
        person_id="y",  # type: ignore[arg-type]
        active_profile_id="z",  # type: ignore[arg-type]
        role="student",
        school_id="s1",  # type: ignore[arg-type]
    )
    scope = await _scope.get_effective_classroom_scope(object(), ctx)
    assert calls["n"] == 1
    assert sorted(scope["effective_classroom_ids"]) == ["c1", "c2", "c3"]


@pytest.mark.asyncio
async def test_admin_scope_is_unrestricted():
    ctx = AuthContext(
        session_id="x",  # type: ignore[arg-type]
        person_id="y",  # type: ignore[arg-type]
        active_profile_id="z",  # type: ignore[arg-type]
        role="admin",
        school_id=None,
    )
    scope = await _scope.get_effective_school_scope(object(), ctx)
    assert scope["effective_school_ids"] is None


@pytest.mark.asyncio
async def test_platform_admin_school_scope_is_unrestricted():
    ctx = AuthContext(
        session_id="00000000-0000-0000-0000-000000000010",  # type: ignore[arg-type]
        person_id="00000000-0000-0000-0000-000000000011",  # type: ignore[arg-type]
        active_profile_id="00000000-0000-0000-0000-000000000012",  # type: ignore[arg-type]
        role="platform_admin",
        school_id=None,
    )
    scope = await _scope.get_effective_school_scope(object(), ctx)
    assert scope["effective_school_ids"] is None
    assert scope["is_admin_like"] is True


@pytest.mark.asyncio
async def test_teacher_scope_uses_my_classrooms_with_group_path(monkeypatch):
    queries = []

    async def fake_fetch_all(_db, sql, params):
        queries.append(sql)
        if "WITH RECURSIVE descendants" in sql:
            return [{"id": "group-school"}, {"id": "child-school"}]
        if "FROM my_classrooms" in sql:
            assert params["pid"] == "teacher-profile"
            assert "school_ids" not in params
            assert "WHERE teacher_id" in sql
            return [{"classroom_id": "c-teacher-1"}, {"classroom_id": "c-teacher-2"}]
        return []

    monkeypatch.setattr(_scope, "fetch_all", fake_fetch_all)
    ctx = AuthContext(
        session_id="s",  # type: ignore[arg-type]
        person_id="p",  # type: ignore[arg-type]
        active_profile_id="teacher-profile",  # type: ignore[arg-type]
        role="teacher",
        school_id="group-school",  # type: ignore[arg-type]
    )
    scope = await _scope.get_effective_classroom_scope(object(), ctx)
    assert scope["effective_classroom_ids"] == ["c-teacher-1", "c-teacher-2"]
    assert any("FROM my_classrooms" in q for q in queries)


@pytest.mark.asyncio
async def test_teacher_scope_leaf_profile_school_in_effective_school_ids(monkeypatch):
    """Perfil na escola leaf: escopo de escola inclui a leaf; turmas do professor filtram por school_id."""

    async def fake_fetch_all(_db, sql, params):
        if "WITH RECURSIVE descendants" in sql:
            # CTE já devolve a âncora; simulamos só a leaf (sem filhos) — merge em get_effective_school_scope deve manter a leaf.
            return [{"id": "11111111-1111-1111-1111-111111111002"}]
        if "FROM my_classrooms" in sql:
            assert params["pid"] == "teacher-leaf"
            return [{"classroom_id": "classroom-at-leaf"}]
        return []

    monkeypatch.setattr(_scope, "fetch_all", fake_fetch_all)
    ctx = AuthContext(
        session_id="s",  # type: ignore[arg-type]
        person_id="p",  # type: ignore[arg-type]
        active_profile_id="teacher-leaf",  # type: ignore[arg-type]
        role="teacher",
        school_id="11111111-1111-1111-1111-111111111002",  # type: ignore[arg-type]
    )
    school_scope = await _scope.get_effective_school_scope(object(), ctx)
    assert school_scope["effective_school_ids"] == ["11111111-1111-1111-1111-111111111002"]

    scope = await _scope.get_effective_classroom_scope(object(), ctx)
    assert scope["effective_classroom_ids"] == ["classroom-at-leaf"]
