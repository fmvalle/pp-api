import uuid

import pytest
from fastapi import HTTPException

from app.v1.auth import service as auth_service


@pytest.mark.asyncio
async def test_select_profile_requires_bootstrap_or_id_token():
    with pytest.raises(HTTPException) as exc:
        await auth_service.select_profile_v1(
            db=object(),  # type: ignore[arg-type]
            profile_id=uuid.uuid4(),
            bootstrap_token=None,
            id_token=None,
            device_info=None,
            ip=None,
            user_agent=None,
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_select_profile_with_invalid_bootstrap(monkeypatch):
    monkeypatch.setattr(auth_service, "decode_bootstrap_token", lambda _t: (_ for _ in ()).throw(ValueError("bad")))
    with pytest.raises(HTTPException) as exc:
        await auth_service.select_profile_v1(
            db=object(),  # type: ignore[arg-type]
            profile_id=uuid.uuid4(),
            bootstrap_token="bad",
            id_token=None,
            device_info=None,
            ip=None,
            user_agent=None,
        )
    assert exc.value.status_code == 401
