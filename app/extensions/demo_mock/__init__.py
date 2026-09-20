"""範例 extension：印度 APMC mandi 的模擬行情。

存在的理由有三個：

1. 當成寫新 extension 時可以照抄的骨架；
2. 沒有網路或官方 API 掛掉時，本機仍能跑完整條 ingest 流程；
3. 測試用固定樣本（同一天同一市場同一品項永遠得到同一個價格）。

雖然是假資料，價格會跟著**產季、季風與休市日**走，而不是純亂數——
亂數曲線在前端看起來很假，也驗證不了漲跌顯示。詳見 `source.py`；
作物與市場清單見 `catalogue.py`。

設定（都是選填）：

    EXTENSIONS_CONFIG={"demo_mock":{
        "volatility_pct": 12,          # 價格波動幅度 ±%
        "markets": ["AZDP", "LSGN"],   # 只產生這些市場，留空 = 全部
        "products": ["IN-ONION"],      # 只產生這些作物，留空 = 全部
        "respect_seasons": true        # false 則產季外也照樣報價
    }}

正式環境請在 `.env` 設 `EXTENSIONS_DISABLED=demo_mock` 關掉它。
"""

from app.extensions.demo_mock.source import DemoMockSource

# registry 認的就是這個名字：每個 extension 套件都必須匯出 SOURCE
SOURCE = DemoMockSource

__all__ = ["SOURCE", "DemoMockSource"]
