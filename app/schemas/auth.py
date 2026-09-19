"""認證相關的 API schema。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.enums import UserRole
from app.schemas.common import ORMModel


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
    # 只在 OTP_DEBUG_ECHO=true 且非正式環境時才有值
    debug_code: str | None = None


class OtpVerifyIn(BaseModel):
    phone: str = Field(min_length=6, max_length=24)
    code: str = Field(min_length=4, max_length=10)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    # 首次登入時可順便指定身分與暱稱
    role: UserRole | None = None
    display_name: str | None = Field(default=None, max_length=80)


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=16)


class LogoutIn(BaseModel):
    refresh_token: str | None = None
    # true 代表登出所有裝置
    all_devices: bool = False


class UserOut(ORMModel):
    id: uuid.UUID
    phone: str
    role: UserRole
    display_name: str | None = None
    country_code: str
    locale: str
    region: str | None = None
    is_active: bool
    can_quote: bool
    created_at: datetime
    last_login_at: datetime | None = None


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=80)
    role: UserRole | None = None
    region: str | None = Field(default=None, max_length=80)
    locale: str | None = Field(default=None, max_length=16)


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int = Field(description="access token 剩餘秒數")
    user: UserOut


class AuthResultOut(TokenOut):
    # 這次驗證是否順帶建立了新帳號，前端可據此導去填暱稱
    is_new_user: bool = False
