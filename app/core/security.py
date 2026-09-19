"""JWT 簽發 / 驗證、雜湊與電話號碼正規化。"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt

from app.core.config import settings
from app.core.errors import AppError, AuthError
from app.data.countries import COUNTRIES, dialing_code

TokenType = Literal["access", "refresh"]

_NON_DIGIT = re.compile(r"[^\d+]")


class InvalidPhoneError(AppError):
    code = "invalid_phone"
    message = "Invalid phone number"


def normalize_phone(raw: str, country_code: str | None = None) -> str:
    """把使用者輸入的號碼統一成 E.164（+886912345678）。

    功能機鍵盤輸入容易帶入空白、破折號或前導 0，這裡一次吸收掉。
    """
    if not raw:
        raise InvalidPhoneError("Phone number is required")

    cleaned = _NON_DIGIT.sub("", raw.strip())
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]

    if not cleaned.startswith("+"):
        # 本地格式（0912345678）要補國碼，就得知道是哪一國。
        # 沒收錄的國家並非不能用，只是必須自己帶 +886 這種完整前綴。
        cc = (country_code or settings.default_country_code).upper()
        dialing = dialing_code(cc)
        if dialing is None:
            raise InvalidPhoneError(
                f"尚未支援的國家代碼 {cc!r}，請改用 E.164 格式（+國碼開頭）輸入號碼",
                details={"country_code": cc, "supported": sorted(COUNTRIES)},
            )
        cleaned = "+" + dialing + cleaned.lstrip("0")

    digits = cleaned[1:]
    if not digits.isdigit() or not 7 <= len(digits) <= 15:
        raise InvalidPhoneError("Phone number must contain 7-15 digits in E.164 form")
    return "+" + digits


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
