"""手機 OTP 登入流程。

流程：
    POST /v1/auth/otp/request  -> 產生驗證碼、寄簡訊
    POST /v1/auth/otp/verify   -> 驗證後建立（或取得）使用者，發 access + refresh token
    POST /v1/auth/refresh      -> 用 refresh token 換新的一組（舊的立刻作廢）

安全上的幾個決定：
- OTP 與 refresh token 都只存 HMAC 雜湊，資料庫外洩也無法直接登入；
- 驗證失敗計數寫在該筆 OTP 上，超過上限就作廢，避免暴力猜碼；
- refresh token 每次使用都旋轉，重複使用舊 token 會被拒絕。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError, AuthError, RateLimitError
from app.core.security import (
    create_token,
    generate_otp,
    generate_refresh_token,
    hash_secret,
)
from app.models.enums import OtpPurpose, UserRole
from app.models.user import OtpCode, RefreshToken, User
from app.services.sms import get_sms_provider

logger = logging.getLogger(__name__)


class OtpRequestResult:
    def __init__(
        self,
        expires_at: datetime,
        retry_after: int,
        debug_code: str | None,
        is_registered: bool,
    ) -> None:
        self.expires_at = expires_at
        self.retry_after = retry_after
        self.debug_code = debug_code
        # 前端據此決定要不要在輸入驗證碼的畫面一起顯示身分選擇
        self.is_registered = is_registered


class RoleRequiredError(AppError):
    """新號碼註冊時沒有指定身分。

    刻意在驗證 OTP 之前就擋下來，這樣驗證碼不會被消耗掉，
    前端補上 role 之後可以直接用同一組碼重試。
    """

    code = "role_required"
    message = "註冊時必須選擇身分：consumer（消費者）/ farmer（小農）/ trader（盤商）"


async def request_otp(
    session: AsyncSession,
    phone: str,
    *,
    purpose: OtpPurpose = OtpPurpose.LOGIN,
    request_ip: str | None = None,
) -> OtpRequestResult:
    now = datetime.now(UTC)

    # 冷卻時間：擋住連點「重寄」
    last = await session.scalar(
        select(OtpCode)
        .where(OtpCode.phone == phone, OtpCode.purpose == purpose)
        .order_by(OtpCode.created_at.desc())
        .limit(1)
    )
    if last is not None:
        elapsed = (now - last.created_at).total_seconds()
        if elapsed < settings.otp_resend_cooldown_seconds:
            wait = int(settings.otp_resend_cooldown_seconds - elapsed)
            raise RateLimitError(
                f"請於 {wait} 秒後再試",
                code="otp_cooldown",
                details={"retry_after": wait},
            )

    # 每小時上限：擋住簡訊費用被刷爆
    hourly = await session.scalar(
        select(func.count())
        .select_from(OtpCode)
        .where(OtpCode.phone == phone, OtpCode.created_at >= now - timedelta(hours=1))
    )
    if (hourly or 0) >= settings.otp_max_per_phone_per_hour:
        raise RateLimitError(
            "此號碼今小時的驗證碼次數已達上限",
            code="otp_hourly_limit",
            details={"retry_after": 3600},
        )

    # 同號碼的舊碼一律作廢，避免多組同時有效
    await session.execute(
        update(OtpCode)
        .where(
            OtpCode.phone == phone,
            OtpCode.purpose == purpose,
            OtpCode.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )

    code = generate_otp()
    expires_at = now + timedelta(seconds=settings.otp_ttl_seconds)
    session.add(
        OtpCode(
            phone=phone,
            code_hash=hash_secret(code),
            purpose=purpose,
            expires_at=expires_at,
            request_ip=request_ip,
        )
    )
    await session.flush()

    minutes = max(1, settings.otp_ttl_seconds // 60)
    text = f"【{settings.sms_sender_id}】驗證碼 {code}，{minutes} 分鐘內有效。請勿轉傳。"
    sent = await get_sms_provider().send(phone, text)
    if not sent:
        logger.error("簡訊發送失敗 phone=%s", phone)

    is_registered = bool(await session.scalar(select(User.id).where(User.phone == phone)))

    return OtpRequestResult(
        expires_at=expires_at,
        retry_after=settings.otp_resend_cooldown_seconds,
        debug_code=code if settings.otp_debug_echo and not settings.is_production else None,
        is_registered=is_registered,
    )


async def verify_otp(
    session: AsyncSession,
    phone: str,
    code: str,
    *,
    purpose: OtpPurpose = OtpPurpose.LOGIN,
) -> None:
    """驗證失敗會丟 AuthError；成功則把該筆標記為已使用。"""
    now = datetime.now(UTC)
    otp = await session.scalar(
        select(OtpCode)
        .where(
            OtpCode.phone == phone,
            OtpCode.purpose == purpose,
            OtpCode.consumed_at.is_(None),
        )
        .order_by(OtpCode.created_at.desc())
        .limit(1)
        .with_for_update()
    )

    if otp is None:
        raise AuthError("請先索取驗證碼", code="otp_not_found")
    if otp.expires_at <= now:
        otp.consumed_at = now
        raise AuthError("驗證碼已過期", code="otp_expired")
    if otp.attempts >= settings.otp_max_attempts:
        otp.consumed_at = now
        raise AuthError("嘗試次數過多，請重新索取驗證碼", code="otp_too_many_attempts")

    otp.attempts += 1
    if otp.code_hash != hash_secret(code):
        remaining = max(0, settings.otp_max_attempts - otp.attempts)
        raise AuthError(
            "驗證碼錯誤",
            code="otp_invalid",
            details={"attempts_remaining": remaining},
        )

    otp.consumed_at = now


async def get_or_create_user(
    session: AsyncSession,
    phone: str,
    *,
    role: UserRole | None = None,
    display_name: str | None = None,
    country_code: str | None = None,
) -> tuple[User, bool]:
    user = await session.scalar(select(User).where(User.phone == phone))
    created = False
    now = datetime.now(UTC)

    if user is None:
        # 註冊：身分必填，而且一旦建立就固定下來
        if role is None:
            raise RoleRequiredError()
        user = User(
            phone=phone,
            role=role,
            display_name=display_name,
            country_code=(country_code or settings.default_country_code).upper(),
            locale=settings.default_locale,
            phone_verified_at=now,
        )
        session.add(user)
        created = True
    else:
        if not user.is_active:
            raise AuthError("此帳號已停用", code="account_disabled")
        # 首次驗證才補上時間；已有值代表之前就驗過了
        user.phone_verified_at = user.phone_verified_at or now
        # 登入：身分已綁定，request 帶什麼都不採用。
        # 想換身分只能透過 PATCH /v1/admin/users/{id}。
        if role is not None and role is not user.role:
            logger.info(
                "登入時帶的 role=%s 與既有身分 %s 不同，已忽略（phone=%s）",
                role.value, user.role.value, phone,
            )
        if display_name and not user.display_name:
            user.display_name = display_name

    user.last_login_at = now
    await session.flush()
    return user, created


class TokenPair:
    def __init__(
        self,
        access_token: str,
        refresh_token: str,
        access_expires_at: datetime,
        refresh_expires_at: datetime,
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.access_expires_at = access_expires_at
        self.refresh_expires_at = refresh_expires_at

    @property
    def expires_in(self) -> int:
        return max(0, int((self.access_expires_at - datetime.now(UTC)).total_seconds()))


async def issue_tokens(
    session: AsyncSession, user: User, *, user_agent: str | None = None
) -> TokenPair:
    access, access_exp = create_token(
        str(user.id), "access", extra_claims={"role": user.role.value}
    )
    refresh = generate_refresh_token()
    refresh_exp = datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days)
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_secret(refresh),
            expires_at=refresh_exp,
            user_agent=(user_agent or "")[:500] or None,
        )
    )
    await session.flush()
    return TokenPair(access, refresh, access_exp, refresh_exp)


async def rotate_refresh_token(
    session: AsyncSession, token: str, *, user_agent: str | None = None
) -> tuple[User, TokenPair]:
    now = datetime.now(UTC)
    row = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_secret(token))
    )
    if row is None:
        raise AuthError("Refresh token 無效", code="invalid_refresh_token")
    if row.revoked_at is not None:
        # 已作廢的 token 又被拿來用，多半是外洩：整條線全部踢掉
        await revoke_all_tokens(session, row.user_id)
        raise AuthError("Refresh token 已作廢，請重新登入", code="refresh_token_reused")
    if row.expires_at <= now:
        raise AuthError("Refresh token 已過期", code="refresh_token_expired")

    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        raise AuthError("帳號不存在或已停用", code="account_disabled")

    row.revoked_at = now
    pair = await issue_tokens(session, user, user_agent=user_agent)
    return user, pair


async def revoke_refresh_token(session: AsyncSession, token: str) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == hash_secret(token), RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


async def revoke_all_tokens(session: AsyncSession, user_id) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


async def phone_is_registered(session: AsyncSession, phone: str) -> bool:
    return bool(await session.scalar(select(User.id).where(User.phone == phone)))


async def admin_set_role(
    session: AsyncSession, user: User, role: UserRole, *, reason: str | None = None
) -> User:
    """維運用：更正使用者身分。

    身分在註冊時綁定，使用者自己改不了，但打錯字這種事還是得有救。
    既有報價的 `role_snapshot` 不會被回溯修改——那是當下的事實紀錄。
    """
    before = user.role
    user.role = role
    await session.flush()
    logger.warning(
        "管理端變更身分：user=%s %s -> %s（原因：%s）",
        user.id, before.value, role.value, reason or "未填",
    )
    return user
