"""品項、市場、資料來源的 API schema。"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.models.catalog import Market, Product, ProductSourceMapping
from app.models.enums import ProductCategory
from app.schemas.common import ORMModel


class ProductNameOut(ORMModel):
    locale: str
    name: str
    is_primary: bool


class ImageCreditOut(BaseModel):
    """圖片與其出處。

    圖多來自 Wikimedia Commons，授權（CC BY / CC BY-SA）**要求標示**
    來源、作者與條款。所以只要畫面上顯示 `url` 這張圖，
    就有義務讓使用者看得到 `license` 與 `author`，或至少提供
    連到 `source_url` 的入口。詳見 API.md 的「圖片與授權」。
    """

    url: str
    source: str | None = None
    source_url: str | None = None
    license: str | None = None
    # 少數圖在 Commons 上沒有標作者；這時靠 source_url 連過去即可
    author: str | None = None

    @classmethod
    def from_model(cls, product: Product) -> "ImageCreditOut | None":
        if not product.image_url:
            return None
        return cls(
            url=product.image_url,
            source=product.image_source,
            source_url=product.image_source_url,
            license=product.image_license,
            author=product.image_author,
        )


class ProductOut(BaseModel):
    """品項。`name` 已依請求的 locale 解析好，前端直接顯示即可。

    清單只給 `image_url`，不帶完整出處——功能機頻寬有限，
    一頁 20 筆各帶一份授權字串太浪費。要顯示出處請取品項詳情。
    """

    id: uuid.UUID
    slug: str
    name: str
    category: ProductCategory
    default_unit: str
    image_url: str | None = None

    @classmethod
    def from_model(cls, product: Product, locale: str) -> "ProductOut":
        return cls(
            id=product.id,
            slug=product.slug,
            name=product.display_name(locale),
            category=product.category,
            default_unit=product.default_unit,
            image_url=product.image_url,
        )


class ProductDetailOut(ProductOut):
    names: list[ProductNameOut] = Field(default_factory=list)
    popularity: int = 0
    # 完整的圖片出處。有 image_url 就一定有這個物件
    image: ImageCreditOut | None = None

    @classmethod
    def from_model(cls, product: Product, locale: str) -> "ProductDetailOut":
        return cls(
            id=product.id,
            slug=product.slug,
            name=product.display_name(locale),
            category=product.category,
            default_unit=product.default_unit,
            image_url=product.image_url,
            names=[ProductNameOut.model_validate(n) for n in product.names],
            popularity=product.popularity,
            image=ImageCreditOut.from_model(product),
        )


class ProductCreate(BaseModel):
    slug: str | None = Field(default=None, max_length=80)
    category: ProductCategory = ProductCategory.OTHER
    default_unit: str = Field(default="kg", max_length=16)
    image_url: str | None = None
    # 有帶 image_url 就應該一併帶出處，否則無法合規地顯示
    image_source: str | None = Field(default=None, max_length=80)
    image_source_url: str | None = None
    image_license: str | None = Field(default=None, max_length=80)
    image_author: str | None = Field(default=None, max_length=200)
    popularity: int = 0
    # 至少一筆；第一筆若沒指定 slug 會用來產生 slug
    names: list[ProductNameOut] = Field(min_length=1)


class MarketOut(BaseModel):
    id: uuid.UUID
    external_id: str | None = None
    name: str
    name_en: str | None = None
    country_code: str
    region: str | None = None
    timezone: str
    latitude: float | None = None
    longitude: float | None = None
    source_key: str | None = None

    @classmethod
    def from_model(cls, market: Market, source_key: str | None = None) -> "MarketOut":
        return cls(
            id=market.id,
            external_id=market.external_id,
            name=market.name,
            name_en=market.name_en,
            country_code=market.country_code,
            region=market.region,
            timezone=market.timezone,
            latitude=market.latitude,
            longitude=market.longitude,
            source_key=source_key,
        )


class RegionOut(BaseModel):
    """有市場資料的地區。

    一定要帶 `country_code`：不只一個國家有資料之後，單看「台中市」和
    「Iganga」是分不出哪個屬於哪一國的，前端也沒辦法分組。
    """

    region: str
    country_code: str
    market_count: int


class SourceOut(BaseModel):
    """一個官方價格 extension 的對外樣貌。"""

    key: str
    name: str
    country_code: str
    currency: str
    timezone: str
    version: str | None = None
    description: str | None = None
    homepage_url: str | None = None
    license: str | None = None
    schedule: str | None = None
    # extension 目前有沒有被載入（資料夾被移除時為 false）
    installed: bool = True
    enabled: bool = True
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None


class SourceLoadErrorOut(BaseModel):
    key: str
    reason: str
    detail: str | None = None


class SourceListOut(BaseModel):
    sources: list[SourceOut]
    # 載入失敗的 extension。服務不會因此停掉，但要看得見
    load_errors: list[SourceLoadErrorOut] = Field(default_factory=list)


class MappingOut(ORMModel):
    """來源代碼 → 平台品項的對照。維運介面用。"""

    id: uuid.UUID
    source_id: uuid.UUID
    external_code: str
    external_name: str | None = None
    product_id: uuid.UUID | None = None
    source_unit: str | None = None
    unit_factor: float
    is_confirmed: bool

    @classmethod
    def from_model(cls, m: ProductSourceMapping) -> "MappingOut":
        return cls.model_validate(m)


class MappingAssign(BaseModel):
    product_id: uuid.UUID
    # 1 個來源單位 = unit_factor 個品項標準單位（例如 1 箱 = 10 kg 就填 10）
    unit_factor: float = Field(default=1.0, gt=0)


class IngestRunOut(ORMModel):
    id: int
    source_key: str
    status: str
    trigger: str
    window_start: date | None = None
    window_end: date | None = None
    records_fetched: int
    records_written: int
    records_skipped: int
    markets_created: int
    mappings_created: int
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    error: str | None = None
