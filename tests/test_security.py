"""電話正規化、JWT 與雜湊的測試。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.errors import AuthError
from app.core.security import (
    InvalidPhoneError,
    create_token,
    decode_token,
    generate_otp,
    hash_secret,
    mask_phone,
    normalize_phone,
    verify_secret,
)


@pytest.mark.parametrize(
    ("raw", "country", "expected"),
    [
        ("0912345678", "TW", "+886912345678"),
        ("0912-345-678", "TW", "+886912345678"),
        ("0912 345 678", "TW", "+886912345678"),
        ("(0912) 345-678", "TW", "+886912345678"),
        ("+886912345678", None, "+886912345678"),
        # 已含國碼但沒有 + 時，libphonenumber 認得出來，不會再補一次國碼
        ("886912345678", "TW", "+886912345678"),
        ("00886912345678", None, "+886912345678"),
        ("09012345678", "JP", "+819012345678"),
        # 烏干達：接 NAMIS 之後必須要能用
        ("0772123456", "UG", "+256772123456"),
        ("+256772123456", None, "+256772123456"),
        # 其他從來沒有手工收錄過的國家也一樣可用
        ("0712345678", "KE", "+254712345678"),
        ("09012345678", "NG", "+2349012345678"),
    ],
)
def test_normalize_phone(raw: str, country: str | None, expected: str) -> None:
    assert normalize_phone(raw, country) == expected


def test_normalize_phone_rejects_numbers_that_are_not_real() -> None:
    """長度對但不是有效號碼的，libphonenumber 會擋下來。

    舊版只檢查「7-15 位數字」，這種明顯不存在的號碼會被放行。
    """
    with pytest.raises(InvalidPhoneError):
        normalize_phone("0000000000", "TW")
    with pytest.raises(InvalidPhoneError):
        normalize_phone("0912345", "TW")      # 台灣手機號碼位數不足


@pytest.mark.parametrize("bad", ["", "123", "abcdefgh", "+1234567890123456789"])
def test_normalize_phone_rejects_garbage(bad: str) -> None:
    with pytest.raises(InvalidPhoneError):
        normalize_phone(bad, "TW")


def test_normalize_phone_unknown_country() -> None:
    with pytest.raises(InvalidPhoneError):
        normalize_phone("0912345678", "ZZ")


def test_mask_phone_keeps_head_and_tail() -> None:
    masked = mask_phone("+886912345678")
    assert masked.startswith("+88")
    assert masked.endswith("678")
    assert "912345" not in masked


def test_otp_hash_roundtrip() -> None:
    code = generate_otp()
    hashed = hash_secret(code)
    assert hashed != code
    assert verify_secret(code, hashed)
    assert not verify_secret("000000", hashed)


def test_generate_otp_length_and_digits() -> None:
    code = generate_otp(8)
    assert len(code) == 8 and code.isdigit()


def test_access_token_roundtrip() -> None:
    token, expires = create_token("user-123", "access", extra_claims={"role": "farmer"})
    payload = decode_token(token, "access")
    assert payload["sub"] == "user-123"
    assert payload["role"] == "farmer"
    assert payload["typ"] == "access"
    assert expires is not None


def test_token_type_is_enforced() -> None:
    token, _ = create_token("user-123", "refresh")
    with pytest.raises(AuthError):
        decode_token(token, "access")


def test_expired_token_is_rejected() -> None:
    token, _ = create_token("user-123", "access", expires_delta=timedelta(seconds=-10))
    with pytest.raises(AuthError) as exc:
        decode_token(token, "access")
    assert exc.value.code == "token_expired"


def test_tampered_token_is_rejected() -> None:
    token, _ = create_token("user-123", "access")
    with pytest.raises(AuthError):
        decode_token(token[:-2] + "xy", "access")
