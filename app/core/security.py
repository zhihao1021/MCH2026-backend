"""JWT 簽發 / 驗證、雜湊與電話號碼正規化。"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
import phonenumbers

from app.core.config import settings
from app.core.errors import AppError, AuthError
from app.data.countries import is_supported_country

TokenType = Literal["access", "refresh"]


class InvalidPhoneError(AppError):
    code = "invalid_phone"
    message = "Invalid phone number"


def normalize_phone(raw: str, country_code: str | None = None) -> str:
    """把使用者輸入的號碼統一成 E.164（+886912345678）。

    交給 libphonenumber（`phonenumbers`）處理，而不是自己寫規則：
    各國的號碼長度、前導 0 要不要去掉、哪些前綴是行動電話，差異非常大，
    自己維護一份規則表遲早會在某個國家上出錯。這樣任何國家都能用。

    功能機鍵盤輸入容易帶入空白、破折號或括號，libphonenumber 會一併吸收。
    """
    if not raw or not raw.strip():
        raise InvalidPhoneError("請輸入電話號碼")

    text = raw.strip()
    # 00 是國際冠碼的另一種寫法，換成 + 讓 libphonenumber 認得
    if text.startswith("00"):
        text = "+" + text[2:]

    region = None
    if not text.startswith("+"):
        # 本地格式（0912345678）要補國碼，就得知道是哪一國
        region = (country_code or settings.default_country_code).upper()
        if not is_supported_country(region):
            raise InvalidPhoneError(
                f"無法辨識的國家代碼 {region!r}，請改用 E.164 格式（+國碼開頭）輸入號碼",
                details={"country_code": region},
            )

    try:
        parsed = phonenumbers.parse(text, region)
    except phonenumbers.NumberParseException as exc:
        raise InvalidPhoneError(
            "電話號碼格式不正確", details={"reason": exc.error_type}
        ) from exc

    if not phonenumbers.is_valid_number(parsed):
        raise InvalidPhoneError("這不是一個有效的電話號碼")

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def mask_phone(phone: str) -> str:
    """回給前端顯示用，不外洩完整號碼。"""
    if len(phone) <= 5:
        return phone
    return phone[:3] + "*" * (len(phone) - 6) + phone[-3:]


def generate_otp(length: int | None = None) -> str:
    length = length or settings.otp_length
    return "".join(secrets.choice("0123456789") for _ in range(length))


def hash_secret(value: str) -> str:
    """OTP / refresh token 的雜湊。加 pepper，並以 hmac 產出定長摘要。"""
    return hmac.new(
        settings.otp_pepper.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_secret(value: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_secret(value), hashed)


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def create_token(
    subject: str,
    token_type: TokenType,
    *,
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    if expires_delta is None:
        expires_delta = (
            timedelta(minutes=settings.access_token_ttl_minutes)
            if token_type == "access"
            else timedelta(days=settings.refresh_token_ttl_days)
        )
    expires_at = now + expires_delta
    payload: dict[str, Any] = {
        "sub": subject,
        "typ": token_type,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    if extra_claims:
        payload.update(extra_claims)
    token = jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)
    return token, expires_at


def decode_token(token: str, expected_type: TokenType | None = None) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Token has expired", code="token_expired") from exc
    except jwt.PyJWTError as exc:
        raise AuthError("Invalid token", code="invalid_token") from exc

    if expected_type and payload.get("typ") != expected_type:
        raise AuthError(f"Expected a {expected_type} token", code="invalid_token_type")
    return payload
