"""個人資料端點。"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.deps import CurrentUser, DbSession, Locale, Paging
from app.core.pagination import Page
from app.schemas.auth import UserOut, UserUpdate
from app.schemas.quote import QuoteOut
from app.services import quotes as quote_service

router = APIRouter(prefix="/me", tags=["me"])


@router.get("", response_model=UserOut, summary="取得個人資料")
async def get_me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.patch("", response_model=UserOut, summary="更新個人資料")
async def update_me(payload: UserUpdate, user: CurrentUser, session: DbSession) -> UserOut:
    """可改暱稱、地區與語系。

    身分（role）不在可改欄位裡——註冊時綁定之後就固定，
    否則報價上的「小農 / 盤商」標示會失去可信度。
    """
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(user, field, value)
    await session.flush()
    return UserOut.model_validate(user)


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
