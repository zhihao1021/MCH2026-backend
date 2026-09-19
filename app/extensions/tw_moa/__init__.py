"""農業部（MOA）農產品交易行情 extension。

資料來自 data.moa.gov.tw 的開放資料 API：
https://data.moa.gov.tw/api/v1/AgriProductsTransType/

注意：未註冊會員時，API 每筆查詢只回傳第一頁（最多 1000 筆），
因此沒有 `api_key` 時會以「逐日、逐市場」的方式切片，繞過上限。
若在 data.moa.gov.tw 註冊會員取得 API key，可放進 `EXTENSIONS_CONFIG`：

    EXTENSIONS_CONFIG={"tw_moa":{"api_key":"你的API金鑰"}}
"""

from app.extensions.tw_moa.source import TwMoaSource

# registry 認的就是這個名字：每個 extension 套件都必須匯出 SOURCE
SOURCE = TwMoaSource

__all__ = ["SOURCE", "TwMoaSource"]
