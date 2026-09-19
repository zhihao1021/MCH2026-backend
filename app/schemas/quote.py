"""報價相關的 API schema。"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from app.core.security import mask_phone
from app.models.enums import QuoteSide, QuoteStatus, UserRole
from app.models.quote import Quote
from app.schemas.catalog import ProductOut


class QuoteCreate(BaseModel):
    product_id: uuid.UUID
    price: Decimal = Field(gt=0, le=Decimal("99999999"))
    side: QuoteSide = QuoteSide.SELL
    unit: str | None = Field(default=None, max_length=16)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    grade: str | None = Field(default=None, max_length=40)
    quantity: Decimal | None = Field(default=None, gt=0)
    min_order: Decimal | None = Field(default=None, gt=0)
    market_id: uuid.UUID | None = None
    region: str | None = Field(default=None, max_length=80)
    location_text: str | None = Field(default=None, max_length=160)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    note: str | None = Field(default=None, max_length=500)
    contact_phone_public: bool = True
    # 幾小時後過期；0 代表不自動過期
    valid_hours: int | None = Field(default=None, ge=0, le=24 * 30)

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str | None) -> str | None:
        return v.upper() if v else v


class QuoteUpdate(BaseModel):
    price: Decimal | None = Field(default=None, gt=0, le=Decimal("99999999"))
    unit: str | None = Field(default=None, max_length=16)
    grade: str | None = Field(default=None, max_length=40)
    quantity: Decimal | None = Field(default=None, gt=0)
    min_order: Decimal | None = Field(default=None, gt=0)
    market_id: uuid.UUID | None = None
    region: str | None = Field(default=None, max_length=80)
    location_text: str | None = Field(default=None, max_length=160)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    note: str | None = Field(default=None, max_length=500)
    contact_phone_public: bool | None = None
    valid_hours: int | None = Field(default=None, ge=0, le=24 * 30)


class QuoteSellerOut(BaseModel):
    """報價者。未公開聯絡方式時 phone 只給遮罩後的字串。"""

    id: uuid.UUID
    display_name: str | None = None
    role: UserRole
    region: str | None = None
    phone: str | None = None
    phone_is_masked: bool = True


class QuoteOut(BaseModel):
    id: uuid.UUID
    product: ProductOut
    side: QuoteSide
    status: QuoteStatus
    price: Decimal
    currency: str
    unit: str
    grade: str | None = None
    quantity: Decimal | None = None
    min_order: Decimal | None = None
    country_code: str
    region: str | None = None
    location_text: str | None = None
    market_id: uuid.UUID | None = None
    note: str | None = None
    seller: QuoteSellerOut
    created_at: datetime
    valid_until: datetime | None = None

    @classmethod
    def from_model(
        cls, quote: Quote, locale: str, *, viewer_id: uuid.UUID | None = None
    ) -> "QuoteOut":
        # 報價者本人永遠看得到完整號碼；其他人要看報價者有沒有同意公開
        is_owner = viewer_id is not None and viewer_id == quote.user_id
        show_full = is_owner or quote.contact_phone_public
        phone = quote.user.phone if show_full else mask_phone(quote.user.phone)

        return cls(
            id=quote.id,
            product=ProductOut.from_model(quote.product, locale),
            side=quote.side,
            status=quote.status,
            price=quote.price,
            currency=quote.currency,
            unit=quote.unit,
            grade=quote.grade,
            quantity=quote.quantity,
            min_order=quote.min_order,
            country_code=quote.country_code,
            region=quote.region,
            location_text=quote.location_text,
            market_id=quote.market_id,
            note=quote.note,
            seller=QuoteSellerOut(
                id=quote.user_id,
                display_name=quote.user.display_name,
                role=quote.role_snapshot,
                region=quote.region,
                phone=phone,
                phone_is_masked=not show_full,
            ),
            created_at=quote.created_at,
            valid_until=quote.valid_until,
        )
