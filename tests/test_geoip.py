"""IP 位置推估的測試（不打外部服務）。"""

from __future__ import annotations

import httpx
import pytest

from app.services.geoip import (
    GeoIpResult,
    IpApiProvider,
    NullGeoIpProvider,
    _mask_ip,
    _match_subdivision,
    is_public_ip,
)


# ---------------------------------------------------------------------------
# IP 判斷
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    ["163.28.10.1", "8.8.8.8", "203.138.180.1", "2001:4860:4860::8888"],
)
def test_public_ips(ip: str) -> None:
    assert is_public_ip(ip) is True


@pytest.mark.parametrize(
    "ip",
    [
        None, "", "not-an-ip",
        "127.0.0.1",        # loopback，本機開發時 client_ip 會是這個
        "10.0.0.1",         # 私有網段
        "192.168.1.1",
        "172.16.0.1",
        "169.254.1.1",      # link-local
        "0.0.0.0",
        "224.0.0.1",        # multicast
        "::1",
    ],
)
def test_non_public_ips(ip: str | None) -> None:
    assert is_public_ip(ip) is False


def test_mask_ip_does_not_leak_full_address() -> None:
    masked = _mask_ip("163.28.10.1")
    assert masked.startswith("163.28")
    assert "10.1" not in masked
    assert _mask_ip("2001:db8::1").endswith(":*")


# ---------------------------------------------------------------------------
# 行政區對照
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("country", "region", "expected"),
    [
        ("TW", "TPE", "TW-TPE"),
        ("tw", "tpe", "TW-TPE"),   # 大小寫不拘
        ("JP", "13", "JP-13"),
        ("TW", "", None),          # ip-api 常常沒給
        ("TW", None, None),
        ("TW", "ZZZ", None),       # 不存在的代碼
        (None, "TPE", None),
        ("XX", "01", None),        # 沒收錄的國家
    ],
)
def test_match_subdivision(country: str | None, region: str | None, expected: str | None) -> None:
    assert _match_subdivision(country, region) == expected


# ---------------------------------------------------------------------------
# GeoIpResult
# ---------------------------------------------------------------------------


def test_empty_result() -> None:
    assert GeoIpResult().is_empty is True
    assert GeoIpResult(country_code="TW").is_empty is False
    assert GeoIpResult(latitude=25.0, longitude=121.5).is_empty is False


# ---------------------------------------------------------------------------
# provider
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_null_provider_is_disabled() -> None:
    p = NullGeoIpProvider()
    assert p.enabled is False
    assert await p.lookup("8.8.8.8") is None


@pytest.mark.anyio
async def test_ip_api_parses_success(monkeypatch) -> None:
    payload = {
        "status": "success", "country": "Taiwan", "countryCode": "TW",
        "region": "TPE", "regionName": "Taipei City", "city": "Taipei",
        "lat": 25.053, "lon": 121.5259, "timezone": "Asia/Taipei",
    }
    _patch_httpx(monkeypatch, httpx.Response(200, json=payload))

    result = await IpApiProvider().lookup("140.112.8.116")
    assert result is not None
    assert result.country_code == "TW"
    assert result.subdivision_code == "TW-TPE"
    assert result.locality == "Taipei"
    assert result.latitude == 25.053
    assert result.timezone == "Asia/Taipei"
    assert result.provider == "ip_api"


@pytest.mark.anyio
async def test_ip_api_handles_fail_status(monkeypatch) -> None:
    """查不到時 ip-api 回 status=fail 而不是 HTTP 錯誤。"""
    _patch_httpx(
        monkeypatch, httpx.Response(200, json={"status": "fail", "message": "reserved range"})
    )
    assert await IpApiProvider().lookup("10.0.0.1") is None


@pytest.mark.anyio
async def test_ip_api_handles_http_error(monkeypatch) -> None:
    """外部服務掛掉不能讓端點爆掉——這只是輔助功能。"""
    _patch_httpx(monkeypatch, httpx.Response(503, text="down"))
    assert await IpApiProvider().lookup("8.8.8.8") is None


@pytest.mark.anyio
async def test_ip_api_handles_timeout(monkeypatch) -> None:
    _patch_httpx(monkeypatch, httpx.ConnectTimeout("timed out"))
    assert await IpApiProvider().lookup("8.8.8.8") is None


@pytest.mark.anyio
async def test_ip_api_handles_garbage_json(monkeypatch) -> None:
    _patch_httpx(monkeypatch, httpx.Response(200, text="<html>not json</html>"))
    assert await IpApiProvider().lookup("8.8.8.8") is None


@pytest.mark.anyio
async def test_ip_api_tolerates_missing_fields(monkeypatch) -> None:
    """只回國碼、沒有座標也沒有行政區的情況要能處理。"""
    _patch_httpx(monkeypatch, httpx.Response(200, json={"status": "success", "countryCode": "TW"}))
    result = await IpApiProvider().lookup("168.95.1.1")
    assert result is not None
    assert result.country_code == "TW"
    assert result.latitude is None
    assert result.subdivision_code is None
    assert result.is_empty is False


def _patch_httpx(monkeypatch, outcome: httpx.Response | Exception) -> None:
    """把 AsyncClient.get 換掉，避免測試真的連外。

    Response 必須綁上 request，否則 raise_for_status() 會丟 RuntimeError
    而不是 HTTPStatusError，測不到我們真正想測的錯誤處理路徑。
    """

    async def fake_get(self, url, **kwargs):  # noqa: ANN001, ANN202
        if isinstance(outcome, Exception):
            raise outcome
        outcome.request = httpx.Request("GET", url)
        return outcome

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
