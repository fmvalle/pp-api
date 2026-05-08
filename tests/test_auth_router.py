import pytest
from fastapi import HTTPException

from app.v1.auth.router import post_logout_v1
from app.v1.auth.schemas import LogoutRequestV1


@pytest.mark.asyncio
async def test_logout_requires_refresh_or_access_context():
    with pytest.raises(HTTPException) as exc:
        await post_logout_v1(
            body=LogoutRequestV1(refresh_token=None),
            db=object(),  # type: ignore[arg-type]
            ctx=None,
        )
    assert exc.value.status_code == 400
