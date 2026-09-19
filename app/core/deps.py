"""FastAPI 依賴注入。"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Header, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.errors import AuthError, ForbiddenError
from app.core.pagination import PageParams, page_params
from app.core.security import decode_token
from app.models.enums import UserRole
from app.models.user import User

# auto_error=False：沒帶 token 時交給我們自己回統一格式的錯誤
bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_db)]
Paging = Annotated[PageParams, Depends(page_params)]


async def _user_from_credentials(
    session: AsyncSession, creds: HTTPAuthorizationCredentials | None
) -> User | None:
    if creds is None or not creds.credentials:
        return None
    payload = decode_token(creds.credentials, "access")
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise AuthError("Token payload 無效", code="invalid_token") from exc

    user = await session.get(User, user_id)
    if user is None:
        raise AuthError("使用者不存在", code="user_not_found")
    if not user.is_active:
        raise AuthError("此帳號已停用", code="account_disabled")
    return user


async def get_current_user(
    session: DbSession,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    user = await _user_from_credentials(session, creds)
    if user is None:
        raise AuthError("請先登入", code="missing_token")
    return user


async def get_optional_user(
    session: DbSession,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User | None:
    """公開端點用：有登入就認出來（例如顯示自己的完整電話），沒登入也能看。"""
    if creds is None:
        return None
    try:
        return await _user_from_credentials(session, creds)
    except AuthError:
        # 過期的 token 不該讓公開內容整個看不到
        return None


async def require_quoter(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    if user.role not in (UserRole.FARMER, UserRole.TRADER):
        raise ForbiddenError(
            "只有小農或盤商身分可以報價，請先到個人設定切換身分",
            code="role_cannot_quote",
        )
    return user


async def require_admin(
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
) -> None:
    """維運端點的保護。沒設定 ADMIN_API_TOKEN 時一律拒絕，避免裸奔。"""
    if not settings.admin_api_token:
        raise ForbiddenError("未設定 ADMIN_API_TOKEN，管理端點已停用", code="admin_disabled")
    if x_admin_token != settings.admin_api_token:
        raise AuthError("X-Admin-Token 不正確", code="invalid_admin_token")


def get_locale(
    locale: Annotated[
        str | None, Query(description="顯示語系，例如 zh-Hant / ja / en")
    ] = None,
    accept_language: Annotated[str | None, Header(alias="Accept-Language")] = None,
) -> str:
    """決定回應要用哪個語系的品項名稱。query 優先，其次 Accept-Language。"""
    if locale:
        return locale
    if accept_language:
        first = accept_language.split(",")[0].split(";")[0].strip()
        if first:
            return first
    return settings.default_locale


def client_ip(request: Request) -> str | None:
    """取得呼叫端 IP。Cloud Phone 走的是遠端瀏覽器，一定在反向代理後面。"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]
QuoterUser = Annotated[User, Depends(require_quoter)]
Locale = Annotated[str, Depends(get_locale)]
ClientIp = Annotated[str | None, Depends(client_ip)]
AdminGuard = Depends(require_admin)
