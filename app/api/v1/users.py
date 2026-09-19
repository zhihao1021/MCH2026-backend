"""個人檔案端點。

分成三塊：
- `/me`          本人的檔案與位置（需登入）
- `/users/{id}`  別人看得到的公開檔案
- 參考資料（國家 / 行政區）在 `app.api.v1.geo`
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.core.deps import CurrentUser, DbSession, Locale, OptionalUser, Paging
from app.core.pagination import Page
from app.schemas.auth import UserOut
from app.schemas.profile import (
    LocationIn,
    LocationOut,
    ProfileUpdate,
    PublicUserOut,
)
from app.schemas.quote import QuoteOut
from app.services import profile as profile_service
from app.services import quotes as quote_service

router = APIRouter(prefix="/me", tags=["me"])
public_router = APIRouter(prefix="/users", tags=["users"])


# ---------------------------------------------------------------------------
# 本人
# ---------------------------------------------------------------------------


@router.get("", response_model=UserOut, summary="取得個人檔案")
async def get_me(user: CurrentUser) -> UserOut:
    return UserOut.from_model(user)


@router.patch("", response_model=UserOut, summary="更新個人檔案")
async def update_me(payload: ProfileUpdate, user: CurrentUser, session: DbSession) -> UserOut:
    """部分更新。明確送 `null` 代表清空該欄位，沒送的欄位不動。

    身分（role）與位置不在這裡：
    身分註冊時綁定（要更正走 `PATCH /v1/admin/users/{id}`），
    位置走 `PUT /v1/me/location`。
    """
    await profile_service.update_profile(session, user, payload)
    return UserOut.from_model(user)


@router.get("/location", response_model=LocationOut, summary="取得自己的位置")
async def get_my_location(user: CurrentUser) -> LocationOut:
    return LocationOut.from_model(user, user.locale)


@router.put("/location", response_model=UserOut, summary="登記 / 更新位置")
async def set_my_location(
    payload: LocationIn, user: CurrentUser, session: DbSession
) -> UserOut:
    """整筆取代：沒帶的欄位會被清空。

    只有 `country_code` 必填。各國地址結構差異很大，
    有收錄行政區清單的國家（見 `GET /v1/geo/countries`）可用 ISO 3166-2 的
    `subdivision_code`，其餘國家改填自由輸入的 `locality` 即可。

    `visibility` 決定別人看得到多少，預設 `region`（只到行政區）。
    """
    await profile_service.set_location(session, user, payload)
    return UserOut.from_model(user)


@router.delete("/location", response_model=UserOut, summary="清除位置")
async def clear_my_location(user: CurrentUser, session: DbSession) -> UserOut:
    """清掉位置，但保留國家——幣別與電話格式都靠它。"""
    await profile_service.clear_location(session, user)
    return UserOut.from_model(user)


@router.get("/quotes", response_model=Page[QuoteOut], summary="我的報價")
async def my_quotes(
    user: CurrentUser,
    session: DbSession,
    paging: Paging,
    locale: Locale,
) -> Page[QuoteOut]:
    items, total = await quote_service.list_quotes(
        session,
        params=paging,
        user_id=user.id,
        status=None,          # 自己的報價連同已下架的一起給
        include_expired=True,
    )
    return Page.build(
        [QuoteOut.from_model(q, locale, viewer_id=user.id) for q in items], total, paging
    )


# ---------------------------------------------------------------------------
# 公開檔案
# ---------------------------------------------------------------------------


@public_router.get("/{user_id}", response_model=PublicUserOut, summary="公開個人檔案")
async def get_public_profile(
    user_id: uuid.UUID,
    session: DbSession,
    locale: Locale,
    viewer: OptionalUser,
) -> PublicUserOut:
    """報價清單點進賣家時看到的內容。

    位置依對方設定的 `location_visibility` 遞減揭露；
    電話不在這裡，那是每一筆報價各自的決定（見 `QuoteOut.seller`）。
    """
    target = await profile_service.get_public_user(session, user_id)
    active = await profile_service.count_active_quotes(session, user_id)

    # 看自己的公開檔案時給完整版，方便使用者確認「別人會看到什麼」之外
    # 也能對照自己填了什麼
    if viewer is not None and viewer.id == target.id:
        return PublicUserOut.from_model(target, target.locale, active_quotes=active)
    return PublicUserOut.from_model(target, locale, active_quotes=active)


@public_router.get("/{user_id}/quotes", response_model=Page[QuoteOut], summary="某人的公開報價")
async def get_public_quotes(
    user_id: uuid.UUID,
    session: DbSession,
    paging: Paging,
    locale: Locale,
    viewer: OptionalUser,
) -> Page[QuoteOut]:
    from app.models.enums import QuoteStatus

    await profile_service.get_public_user(session, user_id)   # 確認存在且啟用
    items, total = await quote_service.list_quotes(
        session, params=paging, user_id=user_id, status=QuoteStatus.ACTIVE
    )
    viewer_id = viewer.id if viewer else None
    return Page.build(
        [QuoteOut.from_model(q, locale, viewer_id=viewer_id) for q in items], total, paging
    )
