"""身分綁定規則的測試。

完整的註冊 / 登入流程需要資料庫，由 `scripts/smoke_test.py` 涵蓋；
這裡測不需要 DB 的部分：schema 的欄位約束與服務層的身分判斷。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.enums import UserRole
from app.schemas.auth import (
    AdminUserRoleUpdate,
    OtpRequestOut,
    OtpVerifyIn,
    UserUpdate,
)
from app.services.auth import RoleRequiredError


def test_user_update_rejects_role() -> None:
    """身分綁定後不能自己改，送 role 要明確報錯而不是被默默忽略。"""
    with pytest.raises(ValidationError) as exc:
        UserUpdate(display_name="阿明", role="trader")  # type: ignore[call-arg]
    assert "role" in str(exc.value)


def test_user_update_allows_editable_fields() -> None:
    u = UserUpdate(display_name="阿明", region="雲林縣", locale="zh-Hant")
    assert u.display_name == "阿明"
    assert "role" not in u.model_dump()


def test_user_update_partial_is_allowed() -> None:
    """PATCH 只送要改的欄位，未送的不應出現在 exclude_unset 的結果裡。"""
    u = UserUpdate(region="嘉義縣")
    assert u.model_dump(exclude_unset=True) == {"region": "嘉義縣"}


@pytest.mark.parametrize("role", ["consumer", "farmer", "trader"])
def test_otp_verify_accepts_all_roles(role: str) -> None:
    payload = OtpVerifyIn(phone="0912345678", code="123456", role=role)  # type: ignore[arg-type]
    assert payload.role is UserRole(role)


def test_otp_verify_role_is_optional_in_schema() -> None:
    """role 的必填與否取決於號碼有沒有帳號，是服務層的判斷，不是 schema 的。"""
    payload = OtpVerifyIn(phone="0912345678", code="123456")
    assert payload.role is None


def test_otp_verify_rejects_unknown_role() -> None:
    with pytest.raises(ValidationError):
        OtpVerifyIn(phone="0912345678", code="123456", role="admin")  # type: ignore[arg-type]


def test_otp_request_out_exposes_registration_state() -> None:
    """前端靠 is_registered 決定要不要顯示身分選擇。"""
    from datetime import UTC, datetime

    out = OtpRequestOut(
        phone="+88*******678",
        expires_at=datetime.now(UTC),
        retry_after=60,
        is_registered=False,
    )
    assert out.is_registered is False
    assert out.debug_code is None


def test_role_required_error_shape() -> None:
    err = RoleRequiredError()
    assert err.code == "role_required"
    assert err.status_code == 400


def test_admin_role_update_requires_role() -> None:
    with pytest.raises(ValidationError):
        AdminUserRoleUpdate()  # type: ignore[call-arg]
    payload = AdminUserRoleUpdate(role=UserRole.TRADER, reason="打錯字")
    assert payload.role is UserRole.TRADER


def test_can_quote_only_for_farmer_and_trader() -> None:
    from app.models.user import User

    for role, expected in [
        (UserRole.CONSUMER, False),
        (UserRole.FARMER, True),
        (UserRole.TRADER, True),
    ]:
        assert User(phone="+886912345678", role=role).can_quote is expected
