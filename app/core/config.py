"""應用組態。所有設定一律由環境變數 / .env 讀入，程式碼內不寫死任何連線資訊。"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 應用 ----
    app_name: str = "AgriPrice API"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    api_prefix: str = "/v1"
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["*"])

    # ---- 資料庫 ----
    # 例：postgresql+asyncpg://user:pass@host:5432/agriprice
    database_url: PostgresDsn = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/agriprice"
    )
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800

    # ---- 安全性 ----
    secret_key: str = Field(default="change-me-in-production", min_length=8)
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 60 * 12
    refresh_token_ttl_days: int = 60
    # OTP 雜湊用的 pepper，與 secret_key 分開以便單獨輪替
    otp_pepper: str = Field(default="change-me-too")

    # ---- OTP ----
    otp_length: int = 6
    otp_ttl_seconds: int = 300
    otp_max_attempts: int = 5
    otp_resend_cooldown_seconds: int = 60
    otp_max_per_phone_per_hour: int = 5
    # 開發用：把 OTP 直接回在 API response，正式環境務必為 false
    otp_debug_echo: bool = False

    default_country_code: str = "TW"
    default_locale: str = "zh-Hant"

    # ---- 簡訊 ----
    sms_provider: Literal["console", "twilio"] = "console"
    sms_sender_id: str = "AgriPrice"
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_number: str | None = None

    # ---- 報價 ----
    quote_default_ttl_hours: int = 48
    quote_max_active_per_user: int = 50

    # ---- 消費者意向價格與防刷（見 ../code_artifact.md）----
    # 目標一：強健統計
    intent_iqr_multiplier: float = Field(default=1.5, ge=0.5, le=5.0)
    intent_trim_fraction: float = Field(default=0.1, ge=0.0, le=0.4)
    # 樣本太少時做 IQR 只會誤殺，低於這個數就不過濾
    intent_min_samples_for_iqr: int = Field(default=8, ge=3)
    # 與樣本數無關的絕對護欄：偏離中位數超過這個倍率就排除。
    # IQR 需要足夠樣本才有意義，但「中位數 850 卻出價 12000」這種
    # 量級的離譜值，三筆樣本時也該擋掉——中位數本身夠穩，拿它當基準安全
    intent_max_median_ratio: float = Field(default=5.0, ge=2.0, le=50.0)
    intent_min_samples_for_ratio_guard: int = Field(default=3, ge=3)
    # 目標一：成本硬約束。底線 = 近 N 日官方行情中位數 x ratio + 物流費
    intent_floor_lookback_days: int = Field(default=7, ge=1, le=90)
    intent_floor_ratio: float = Field(default=0.7, ge=0.0, le=2.0)
    intent_floor_logistics: float = Field(default=0.0, ge=0.0)
    # 目標二：行為摩擦
    intent_cooldown_days: int = Field(default=7, ge=0, le=365)
    intent_geofence_km: float = Field(default=15.0, ge=0.0)
    intent_block_hosting_ip: bool = True

    # ---- 消費者回報的零售價 ----
    # 看板只看近期回報。零售價變動快，兩週前的標價已經沒有參考價值
    retail_lookback_days: int = Field(default=14, ge=1, le=365)
    # 同一人 × 同一品項 × 同一店家的冷卻期。零售回報是觀察不是意願，
    # 同一個人本來就可能在多家店看到不同價格，所以冷卻期綁到店家層級，
    # 而且比意向價格（7 天）短得多——超市改價本來就頻繁
    retail_cooldown_hours: int = Field(default=24, ge=0, le=8760)
    # 允許補登多久以前看到的價格。太舊的無從查證，也拉不回當期行情
    retail_max_observation_age_days: int = Field(default=7, ge=0, le=90)
    # 目標三：信譽權重
    intent_weight_initial: float = Field(default=1.0, ge=0.0, le=2.0)
    intent_weight_min: float = Field(default=0.0, ge=0.0)
    intent_weight_max: float = Field(default=2.0, ge=1.0)
    # 上調慢、下調快：錯殺正常使用者的代價遠低於讓刷票者維持高權重
    intent_weight_step_up: float = Field(default=0.05, gt=0)
    intent_weight_step_down: float = Field(default=0.25, gt=0)
    intent_consensus_band_pct: float = Field(default=15.0, gt=0)
    intent_deviation_sigma: float = Field(default=2.0, gt=0)
    intent_min_samples_for_reputation: int = Field(default=5, ge=2)
    intent_shadow_ban_strikes: int = Field(default=3, ge=1)
    intent_shadow_ban_weight: float = Field(default=0.0, ge=0.0)
    # 目標四：收到這麼多次推播卻零響應，視為幽靈需求
    intent_ghost_notification_threshold: int = Field(default=5, ge=1)

    # ---- Demo 用的國家範圍 ----
    # 讀取層的過濾：只影響市場 / 行情 / 地區 / 報價 / 資料來源的查詢結果，
    # 不影響註冊與寫入，也不刪任何資料。白名單優先。
    # 例：DEMO_VISIBLE_COUNTRIES=UG 只顯示烏干達；DEMO_HIDDEN_COUNTRIES=TW 只藏台灣
    demo_visible_countries: Annotated[list[str], NoDecode] = Field(default_factory=list)
    demo_hidden_countries: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ---- IP 反查位置 ----
    # Cloud Phone 不支援 Geolocation API，所以「取得目前位置」只能靠
    # X-Forwarded-For 裡的真實 IP 反查。結果是城市級的推估，僅供建議。
    # none = 關閉（偵測端點回 501，使用者仍可手動輸入座標）
    # ip_api = ip-api.com，免金鑰但只有 HTTP，IP 會明文送給第三方
    geoip_provider: Literal["none", "ip_api"] = "ip_api"
    geoip_timeout: float = 8.0

    # ---- Extension ----
    extensions_dir: Path = BASE_DIR / "app" / "extensions"
    # 留空 = 載入目錄下所有 extension；填了就只載入清單內的
    extensions_enabled: Annotated[list[str], NoDecode] = Field(default_factory=list)
    extensions_disabled: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # 每個 extension 的組態：{"tw_moa": {"api_key": "..."}}
    extensions_config: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # 每累積這麼多筆就寫入並提交一次。調小 = 資料更快落地、失敗時損失更少；
    # 調大 = 交易次數少、整體吞吐高。
    ingest_batch_size: int = Field(default=500, ge=1, le=10000)
    extension_http_timeout: float = 30.0
    extension_user_agent: str = "AgriPriceBot/0.1 (+https://example.org/agriprice)"

    # ---- 排程 ----
    scheduler_enabled: bool = True
    scheduler_timezone: str = "UTC"
    # 首次啟動時是否立即補跑一次所有來源
    scheduler_run_on_startup: bool = False

    # ---- 維運 ----
    admin_api_token: str | None = None
    log_level: str = "INFO"
    log_json: bool = False

    @field_validator(
        "cors_origins",
        "extensions_enabled",
        "extensions_disabled",
        "demo_visible_countries",
        "demo_hidden_countries",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, v: Any) -> Any:
        """允許 .env 以逗號分隔字串或 JSON 陣列兩種寫法。"""
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("["):
                return json.loads(v)
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("extensions_config", mode="before")
    @classmethod
    def _parse_json_obj(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            return json.loads(v) if v else {}
        return v

    @property
    def sqlalchemy_url(self) -> str:
        return str(self.database_url)

    @property
    def sync_sqlalchemy_url(self) -> str:
        """Alembic / 工具用的同步連線字串。"""
        return str(self.database_url).replace("+asyncpg", "+psycopg2")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
