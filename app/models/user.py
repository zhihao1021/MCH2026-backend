"""使用者與認證憑證。登入方式是手機號碼 + OTP，因此沒有密碼欄位。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, pg_enum, uuid_pk
from app.models.enums import LocationVisibility, OtpPurpose, UnitSystem, UserRole

if TYPE_CHECKING:
    from app.models.quote import Quote


class User(Base, TimestampMixin):
    """使用者帳號、個人檔案與所在位置。

    位置刻意拆成「結構化代碼 + 自由文字」兩層：
    有收錄行政區清單的國家（見 `app.data.countries`）用 ISO 3166-2 的
    `subdivision_code`，沒收錄的國家就只填自由輸入的 `locality`。
    這樣新增一個國家不必改結構，也不會被某一國的行政區劃綁死。
    """

    __tablename__ = "users"
    __table_args__ = (
        # 依地區找附近的小農 / 盤商
        Index("ix_users_country_code_subdivision_code", "country_code", "subdivision_code"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    # E.164，例如 +886912345678
    phone: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    role: Mapped[UserRole] = mapped_column(
        pg_enum(UserRole, "user_role"),
        nullable=False,
        default=UserRole.CONSUMER,
    )

    # ---- 個人檔案 ----
    display_name: Mapped[str | None] = mapped_column(String(80))
    # 農場名 / 商號，報價清單上會與暱稱一起顯示
    business_name: Mapped[str | None] = mapped_column(String(120))
    bio: Mapped[str | None] = mapped_column(Text)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    website_url: Mapped[str | None] = mapped_column(Text)

    # ---- 在地化偏好 ----
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, default="TW")
    locale: Mapped[str] = mapped_column(String(16), nullable=False, default="zh-Hant")
    # 兩者都可為 NULL，代表「跟著所在國家的預設走」。
    # 存成 NULL 而不是在註冊時複製一份，使用者搬到別的國家時才會自動跟著變，
    # 而明確設定過的人也不會被覆寫。解析見 app.schemas.profile 的 effective_* 函式。
    preferred_currency: Mapped[str | None] = mapped_column(String(3))
    unit_system: Mapped[UnitSystem | None] = mapped_column(pg_enum(UnitSystem, "unit_system"))
    # IANA 時區，例如 Asia/Taipei
    timezone: Mapped[str | None] = mapped_column(String(64))

    # ---- 位置 ----
    # ISO 3166-2，例如 TW-YUN / JP-13。沒有收錄清單的國家留空
    subdivision_code: Mapped[str | None] = mapped_column(String(8))
    # 市 / 鎮 / 區。自由輸入，任何國家都適用
    locality: Mapped[str | None] = mapped_column(String(120))
    # 街道地址。只有 location_visibility=exact 才會對外顯示
    address_line: Mapped[str | None] = mapped_column(String(200))
    # 郵遞區號格式各國差異太大，一律當字串存不做格式驗證
    postal_code: Mapped[str | None] = mapped_column(String(16))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    location_visibility: Mapped[LocationVisibility] = mapped_column(
        pg_enum(LocationVisibility, "location_visibility"),
        nullable=False,
        default=LocationVisibility.REGION,
    )
    location_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ---- 聯絡方式 ----
    # 新報價預設要不要公開電話；個別報價仍可自行覆寫
    contact_phone_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    quotes: Mapped[list["Quote"]] = relationship(back_populates="user")

    @property
    def can_quote(self) -> bool:
        return self.role in (UserRole.FARMER, UserRole.TRADER)

    @property
    def has_location(self) -> bool:
        """有沒有填過任何位置資訊。前端據此提示使用者去補。"""
        return any(
            (self.subdivision_code, self.locality, self.postal_code, self.address_line)
        ) or self.latitude is not None


class OtpCode(Base):
    """一次性驗證碼。只存雜湊，明碼僅存在於簡訊中。"""

    __tablename__ = "otp_codes"
    __table_args__ = (Index("ix_otp_codes_phone_created_at", "phone", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    purpose: Mapped[OtpPurpose] = mapped_column(
        pg_enum(OtpPurpose, "otp_purpose"),
        nullable=False,
        default=OtpPurpose.LOGIN,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_ip: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RefreshToken(Base):
    """Refresh token 只存雜湊；驗證時比對雜湊並旋轉（每次換新的）。"""

    __tablename__ = "refresh_tokens"
    __table_args__ = (Index("ix_refresh_tokens_user_id_expires_at", "user_id", "expires_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship()
