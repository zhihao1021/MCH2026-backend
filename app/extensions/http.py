"""給 extension 用的共用 HTTP client。

所有 extension 共用同一個連線池，並統一帶上 User-Agent 與逾時設定，
避免每個來源各寫一份、也方便日後集中加上代理或速率限制。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# 這些狀態碼重試通常有用；4xx（除了 429）重試沒有意義
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def build_client(**overrides: object) -> httpx.AsyncClient:
    kwargs: dict[str, object] = {
        "timeout": httpx.Timeout(settings.extension_http_timeout),
        "headers": {
            "User-Agent": settings.extension_user_agent,
            "Accept-Encoding": "gzip, deflate",
        },
        "follow_redirects": True,
        "limits": httpx.Limits(max_connections=20, max_keepalive_connections=10),
    }
    kwargs.update(overrides)
    return httpx.AsyncClient(**kwargs)  # type: ignore[arg-type]


@asynccontextmanager
async def http_client(**overrides: object) -> AsyncIterator[httpx.AsyncClient]:
    client = build_client(**overrides)
    try:
        yield client
    finally:
        await client.aclose()


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    attempts: int = 3,
    backoff: float = 1.5,
    **kwargs: object,
) -> httpx.Response:
    """帶指數退避的請求。最後一次仍失敗就把例外往外丟。

    Extension 不一定要用這個；只是多數官方開放資料站台都不太穩，
    有個現成的重試可以少寫很多樣板。
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = await client.request(method, url, **kwargs)  # type: ignore[arg-type]
            if response.status_code in RETRY_STATUS and attempt < attempts:
                delay = _retry_delay(response, attempt, backoff)
                logger.warning(
                    "%s %s 回 %s，%.1fs 後重試（第 %d/%d 次）",
                    method,
                    url,
                    response.status_code,
                    delay,
                    attempt,
                    attempts,
                )
                await asyncio.sleep(delay)
                continue
            response.raise_for_status()
            return response
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            delay = backoff**attempt
            logger.warning(
                "%s %s 失敗（%s），%.1fs 後重試（第 %d/%d 次）",
                method,
                url,
                type(exc).__name__,
                delay,
                attempt,
                attempts,
            )
            await asyncio.sleep(delay)

    assert last_exc is not None
    raise last_exc


def _retry_delay(response: httpx.Response, attempt: int, backoff: float) -> float:
    """優先尊重伺服器給的 Retry-After，其次才用指數退避。"""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(float(retry_after), 60.0)
        except ValueError:
            pass
    return backoff**attempt
