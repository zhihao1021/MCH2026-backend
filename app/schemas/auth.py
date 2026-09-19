"""認證相關的 API schema。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.enums import UnitSystem, UserRole
from app.models.user import User
from app.schemas.common import ORMModel
from app.schemas.profile import (
    LocationOut,
    ProfileUpdate,
    effective_currency,
    effective_unit_system,
)

# 個人檔案的可編輯欄位定義在 app.schemas.profile。
# 這裡重新匯出，讓既有的 `from app.schemas.auth import UserUpdate` 仍然可用。
UserUpdate = ProfileUpdate


class OtpRequestIn(BaseModel):
    # 可以是 E.164（+886912345678）或本地格式（0912345678）＋ country_code
    phone: str = Field(min_length=6, max_length=24)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)

    @field_validator("country_code")
    @classmethod
    def _upper(cls, v: str | None) -> str | None:
        return v.upper() if v else v


class OtpRequestOut(BaseModel):
    # 遮罩後的號碼，讓使用者確認自己沒打錯
    phone: str
    expires_at: datetime
    retry_after: int = Field(description="幾秒後才能重新索取驗證碼")
    # 這個號碼是否已經有帳號。false 代表接下來的 verify 屬於「註冊」，
    # 前端要在輸入驗證碼的同一畫面讓使用者選身分（role 為必填）
    is_registered: bool = Field(
        description="false = 註冊流程，verify 時必須帶 role；true = 登入流程，role 會被忽略"
    )
    # 只在 OTP_DEBUG_ECHO=true 且非正式環境時才有值
    debug_code: str | None = None


class OtpVerifyIn(BaseModel):
    phone: str = Field(min_length=6, max_length=24)
    code: str = Field(min_length=4, max_length=10)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    # 註冊（號碼還沒有帳號）時必填，且一旦建立就綁定，使用者不能自行更改。
    # 既有帳號登入時這個欄位會被忽略。
    role: UserRole | None = Field(
        default=None,
        description="註冊時必填：consumer / farmer / trader。既有帳號登入時忽略",
    )
    display_name: str | None = Field(default=None, max_length=80)


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=16)


class LogoutIn(BaseModel):
    refresh_token: str | None = None
    # true 代表登出所有裝置
    all_devices: bool = False


class UserOut(ORMModel):
    """本人視角的完整個人檔案。

    位置一律附在這裡，讓登入回應就帶齊前端要的東西，
    功能機不必為了顯示「你的所在地」再打一支 API。
    """

    id: uuid.UUID
    phone: str
    role: UserRole

    display_name: str | None = None
    business_name: str | None = None
    bio: str | None = None
    avatar_url: str | None = None
    website_url: str | None = None

    country_code: str
    locale: str
    # 使用者明確設定的值；null 代表「跟著國家預設走」
    preferred_currency: str | None = None
    unit_system: UnitSystem | None = None
    # 實際該用的值，已把國家預設套進去，前端直接用這兩個
    currency: str
    effective_unit_system: UnitSystem
    timezone: str | None = None

    location: LocationOut
    has_location: bool

    contact_phone_public: bool
    is_active: bool
    can_quote: bool
    created_at: datetime
    last_login_at: datetime | None = None

    @classmethod
    def from_model(cls, user: User) -> "UserOut":
        # 用使用者自己的語系解析國名與行政區名，不看請求的 Accept-Language：
        # 這是「我的資料」，該照他自己的設定顯示
        locale = user.locale
        return cls(
            id=user.id,
            phone=user.phone,
            role=user.role,
            display_name=user.display_name,
            business_name=user.business_name,
            bio=user.bio,
            avatar_url=user.avatar_url,
            website_url=user.website_url,
            country_code=user.country_code,
            locale=user.locale,
            preferred_currency=user.preferred_currency,
            unit_system=user.unit_system,
            currency=effective_currency(user),
            effective_unit_system=effective_unit_system(user),
            timezone=user.timezone,
            location=LocationOut.from_model(user, locale),
            has_location=user.has_location,
            contact_phone_public=user.contact_phone_public,
            is_active=user.is_active,
            can_quote=user.can_quote,
            created_at=user.created_at,
            last_login_at=user.last_login_at,
        )


class AdminUserRoleUpdate(BaseModel):
    """維運用：更正使用者身分。一般使用者無法自行呼叫。"""

    role: UserRole
    reason: str | None = Field(default=None, max_length=200, description="留在日誌裡的原因")


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int = Field(description="access token 剩餘秒數")
    user: UserOut


class AuthResultOut(TokenOut):
    # 這次驗證是否順帶建立了新帳號，前端可據此導去填暱稱
    is_new_user: bool = False
