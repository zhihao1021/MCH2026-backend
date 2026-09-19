"""個人檔案與位置的商業邏輯。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.data.countries import default_timezone
from app.models.enums import QuoteStatus
from app.models.quote import Quote
from app.models.user import User
from app.schemas.profile import LocationIn, ProfileUpdate

# 換國家時要一併清掉的欄位。留著舊國家的行政區代碼比留空更糟，
# 因為 TW-YUN 掛在 country_code=JP 底下是查不到也顯示不出來的髒資料。
_LOCATION_FIELDS = (
    "subdivision_code",
    "locality",
    "address_line",
    "postal_code",
    "latitude",
    "longitude",
)


async def set_location(session: AsyncSession, user: User, payload: LocationIn) -> User:
    """登記 / 取代位置。

    這是 PUT 語意：沒帶的欄位一律清空，不是部分更新。
    位置資料容易殘留（改了縣市但忘了改郵遞區號），整筆取代最不會出錯。
    """
    user.country_code = payload.country_code
    user.subdivision_code = payload.subdivision_code
    user.locality = payload.locality
    user.address_line = payload.address_line
    user.postal_code = payload.postal_code
    user.latitude = payload.latitude
    user.longitude = payload.longitude
    user.location_visibility = payload.visibility
    # 沒指定時給該國的預設時區，讓「今日行情」的日界線至少是對的
    user.timezone = payload.timezone or default_timezone(payload.country_code)
    user.location_updated_at = datetime.now(UTC)

    await session.flush()
    return user


async def clear_location(session: AsyncSession, user: User) -> User:
    """清除位置，只留下國家（國家是幣別與電話格式的依據，不能沒有）。"""
    for field in _LOCATION_FIELDS:
        setattr(user, field, None)
    user.location_updated_at = None
    await session.flush()
    return user


async def update_profile(session: AsyncSession, user: User, payload: ProfileUpdate) -> User:
    """更新個人檔案。

    用 `exclude_unset` 而不是 `exclude_none`：使用者明確送 `null`
    代表「清空這個欄位」，沒送才是「不要動」。
    """
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    await session.flush()
    return user


async def get_public_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise NotFoundError("User not found", code="user_not_found")
    return user


async def count_active_quotes(session: AsyncSession, user_id: uuid.UUID) -> int:
    total = await session.scalar(
        select(func.count())
        .select_from(Quote)
        .where(
            Quote.user_id == user_id,
            Quote.status == QuoteStatus.ACTIVE,
            or_(Quote.valid_until.is_(None), Quote.valid_until > datetime.now(UTC)),
        )
    )
    return total or 0
