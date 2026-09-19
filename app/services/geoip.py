"""以 IP 推估使用者位置。

## 為什麼不是用瀏覽器的 Geolocation API

前端跑在 Cloud Phone 上——那是**遠端渲染**的瀏覽器，頁面在 CloudMosa 的
機房執行、只把畫面串到手機。官方文件明講
「Cloud Phone does not offer access to device hardware for local
connectivity or positioning」，Geolocation API 列在不支援清單裡。
就算能呼叫，拿到的也會是機房的座標而不是使用者的。

官方給的替代做法是用 IP 反查，而使用者的真實 IP 會放在
`X-Forwarded-For`（`app.core.deps.client_ip` 已經在讀了）——
機房自己的 IP 才是連線的 remote address。

## 精度

IP 反查是**城市級**的，誤差動輒數十公里，而且在行動網路上常常指到
電信商的出口機房。所以這裡的結果一律當作「建議值」，
要由使用者確認後才寫進 `PUT /v1/me/location`，不會自動存。

## 隱私

查詢會把使用者 IP 送給第三方服務。ip-api.com 的免費方案只有 HTTP
（沒有 TLS），所以 IP 是明文傳送的。這是一個取捨：
不接受的話設 `GEOIP_PROVIDER=none`，偵測端點就會回 501，
使用者仍可手動輸入座標。
"""

from __future__ import annotations

import abc
import ipaddress
import logging
from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.data.countries import is_supported_country, list_subdivisions

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GeoIpResult:
    """IP 反查的結果。所有欄位都可能是 None——這是推估，不是量測。"""

    latitude: float | None = None
    longitude: float | None = None
    country_code: str | None = None
    # ISO 3166-2，例如 TW-TPE。只有在我們有收錄該國行政區時才會有值
    subdivision_code: str | None = None
    # 來源給的行政區名稱，供對不到代碼時顯示
    subdivision_name: str | None = None
    locality: str | None = None
    timezone: str | None = None
    provider: str = "none"

    @property
    def is_empty(self) -> bool:
        return self.latitude is None and self.country_code is None


class GeoIpProvider(abc.ABC):
    """新增供應商就實作這個介面，再到 get_geoip_provider() 註冊。"""

    name: str = "base"
    enabled: bool = True

    @abc.abstractmethod
    async def lookup(self, ip: str) -> GeoIpResult | None:
        """查不到或失敗一律回 None，不要丟例外——這只是輔助功能。"""


class NullGeoIpProvider(GeoIpProvider):
    """關閉狀態。端點會據此回 501。"""

    name = "none"
    enabled = False

    async def lookup(self, ip: str) -> GeoIpResult | None:
        return None


class IpApiProvider(GeoIpProvider):
    """ip-api.com。免費、免金鑰，但只有 HTTP 且每分鐘 45 次。"""

    name = "ip_api"
    ENDPOINT = "http://ip-api.com/json/{ip}"
    FIELDS = "status,message,country,countryCode,region,regionName,city,lat,lon,timezone"

    async def lookup(self, ip: str) -> GeoIpResult | None:
        try:
            async with httpx.AsyncClient(timeout=settings.geoip_timeout) as client:
                resp = await client.get(
                    self.ENDPOINT.format(ip=ip), params={"fields": self.FIELDS}
                )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            logger.warning("IP 反查失敗 ip=%s", _mask_ip(ip), exc_info=True)
            return None

        if data.get("status") != "success":
            logger.info("IP 反查無結果 ip=%s：%s", _mask_ip(ip), data.get("message"))
            return None

        country = (data.get("countryCode") or "").upper() or None
        return GeoIpResult(
            latitude=_as_float(data.get("lat")),
            longitude=_as_float(data.get("lon")),
            country_code=country,
            subdivision_code=_match_subdivision(country, data.get("region")),
            subdivision_name=(data.get("regionName") or "").strip() or None,
            locality=(data.get("city") or "").strip() or None,
            timezone=(data.get("timezone") or "").strip() or None,
            provider=self.name,
        )


_PROVIDERS: dict[str, type[GeoIpProvider]] = {
    "none": NullGeoIpProvider,
    "ip_api": IpApiProvider,
}

_instance: GeoIpProvider | None = None


def get_geoip_provider() -> GeoIpProvider:
    global _instance
    if _instance is None:
        cls = _PROVIDERS.get(settings.geoip_provider, NullGeoIpProvider)
        _instance = cls()
        logger.info("IP 反查供應商：%s", _instance.name)
    return _instance


def reset_geoip_provider() -> None:
    """測試用。"""
    global _instance
    _instance = None


def is_public_ip(ip: str | None) -> bool:
    """私有 / 迴圈 / 保留位址查了也沒意義，先擋掉省一次請求。

    本機開發時 client_ip 會是 127.0.0.1，這裡回 False，
    端點就能回一個看得懂的錯誤而不是「查無結果」。
    """
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _match_subdivision(country_code: str | None, region: str | None) -> str | None:
    """把來源給的行政區代碼對到我們收錄的 ISO 3166-2。

    ip-api 的 `region` 就是 ISO 3166-2 的後綴（TPE、13），
    但不是每個 IP 都有，而且我們沒收錄的國家也對不到。
    """
    if not country_code or not region:
        return None
    if not is_supported_country(country_code):
        return None
    candidate = f"{country_code.upper()}-{region.strip().upper()}"
    for sub in list_subdivisions(country_code):
        if sub.code == candidate:
            return sub.code
    return None


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _mask_ip(ip: str) -> str:
    """記 log 時別留完整 IP。"""
    if ":" in ip:
        return ip.rsplit(":", 2)[0] + ":*"
    parts = ip.split(".")
    return ".".join(parts[:2] + ["*", "*"]) if len(parts) == 4 else "*"
