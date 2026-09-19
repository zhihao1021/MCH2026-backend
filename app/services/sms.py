"""簡訊發送。抽成介面，之後換供應商不用動到 OTP 邏輯。"""

from __future__ import annotations

import abc
import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class SmsProvider(abc.ABC):
    """新增供應商就實作這個介面，再到 get_sms_provider() 註冊。"""

    name: str = "base"

    @abc.abstractmethod
    async def send(self, to: str, text: str) -> bool:
        """送出簡訊。回傳是否成功；不要因為送不出去就丟例外中斷登入流程。"""


class ConsoleSmsProvider(SmsProvider):
    """開發用：把簡訊印到日誌。"""

    name = "console"

    async def send(self, to: str, text: str) -> bool:
        logger.info("[SMS:console] to=%s text=%s", to, text)
        return True


class TwilioSmsProvider(SmsProvider):
    """Twilio Messages API。只用 REST，不拉 SDK 進來。"""

    name = "twilio"

    def __init__(self) -> None:
        missing = [
            n
            for n, v in (
                ("TWILIO_ACCOUNT_SID", settings.twilio_account_sid),
                ("TWILIO_AUTH_TOKEN", settings.twilio_auth_token),
                ("TWILIO_FROM_NUMBER", settings.twilio_from_number),
            )
            if not v
        ]
        if missing:
            raise RuntimeError(f"SMS_PROVIDER=twilio 但缺少設定：{', '.join(missing)}")
        self._url = (
            f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Messages.json"
        )
        self._auth = (settings.twilio_account_sid or "", settings.twilio_auth_token or "")

    async def send(self, to: str, text: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    self._url,
                    auth=self._auth,
                    data={"To": to, "From": settings.twilio_from_number, "Body": text},
                )
            if resp.status_code >= 400:
                logger.error("Twilio 回 %s：%s", resp.status_code, resp.text[:500])
                return False
            return True
        except httpx.HTTPError:
            logger.exception("Twilio 請求失敗")
            return False


_PROVIDERS: dict[str, type[SmsProvider]] = {
    "console": ConsoleSmsProvider,
    "twilio": TwilioSmsProvider,
}

_instance: SmsProvider | None = None


def get_sms_provider() -> SmsProvider:
    global _instance
    if _instance is None:
        cls = _PROVIDERS.get(settings.sms_provider, ConsoleSmsProvider)
        _instance = cls()
        logger.info("簡訊供應商：%s", _instance.name)
    return _instance


def reset_sms_provider() -> None:
    """測試用。"""
    global _instance
    _instance = None
