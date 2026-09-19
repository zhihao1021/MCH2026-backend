"""認證端點：手機 OTP 登入。"""

from __future__ import annotations

from fastapi import APIRouter, Header, Request, status
from typing import Annotated

from app.core.deps import ClientIp, CurrentUser, DbSession
from app.core.security import mask_phone, normalize_phone
from app.schemas.auth import (
    AuthResultOut,
    LogoutIn,
    OtpRequestIn,
    OtpRequestOut,
    OtpVerifyIn,
    RefreshIn,
    TokenOut,
    UserOut,
)
from app.schemas.common import Ack
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/otp/request",
    response_model=OtpRequestOut,
    summary="索取簡訊驗證碼",
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_otp(payload: OtpRequestIn, session: DbSession, ip: ClientIp) -> OtpRequestOut:
    phone = normalize_phone(payload.phone, payload.country_code)
    result = await auth_service.request_otp(session, phone, request_ip=ip)
    return OtpRequestOut(
        phone=mask_phone(phone),
        expires_at=result.expires_at,
        retry_after=result.retry_after,
        is_registered=result.is_registered,
        debug_code=result.debug_code,
    )


@router.post("/otp/verify", response_model=AuthResultOut, summary="驗證並登入 / 註冊")
async def verify_otp(
    payload: OtpVerifyIn,
    session: DbSession,
    user_agent: Annotated[str | None, Header(alias="User-Agent")] = None,
) -> AuthResultOut:
    phone = normalize_phone(payload.phone, payload.country_code)

    # 先擋沒帶身分的註冊，再驗證驗證碼——順序是刻意的：
    # 這樣驗證碼不會被消耗掉，前端補上 role 後可以用同一組碼重試。
    if payload.role is None and not await auth_service.phone_is_registered(session, phone):
        raise auth_service.RoleRequiredError()

    await auth_service.verify_otp(session, phone, payload.code)
    user, created = await auth_service.get_or_create_user(
        session,
        phone,
        role=payload.role,
        display_name=payload.display_name,
        country_code=payload.country_code,
    )
    tokens = await auth_service.issue_tokens(session, user, user_agent=user_agent)
    return AuthResultOut(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
        user=UserOut.from_model(user),
        is_new_user=created,
    )


@router.post("/refresh", response_model=TokenOut, summary="換發 token")
async def refresh(
    payload: RefreshIn,
    session: DbSession,
    user_agent: Annotated[str | None, Header(alias="User-Agent")] = None,
) -> TokenOut:
    user, tokens = await auth_service.rotate_refresh_token(
        session, payload.refresh_token, user_agent=user_agent
    )
    return TokenOut(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
        user=UserOut.from_model(user),
    )


@router.post("/logout", response_model=Ack, summary="登出")
async def logout(payload: LogoutIn, session: DbSession, user: CurrentUser) -> Ack:
    if payload.all_devices:
        await auth_service.revoke_all_tokens(session, user.id)
        return Ack(message="已登出所有裝置")
    if payload.refresh_token:
        await auth_service.revoke_refresh_token(session, payload.refresh_token)
    return Ack(message="已登出")
