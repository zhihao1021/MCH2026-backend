"""認證相關的 API schema。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    """可以自行修改的個人資料。

    `role` 不在這裡：身分在註冊時綁定，之後不能自己改。
    報價會帶上 `role_snapshot`，讓身分可以被信任；
    若能隨時切換，「這是小農報的價」就失去意義了。
    要更正身分得走 `PATCH /v1/admin/users/{id}`。
    """

    # 明確拒絕未知欄位：前端若還在送 role，會收到 422 而不是被默默忽略
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    locale: str | None = Field(default=None, max_length=16)


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
