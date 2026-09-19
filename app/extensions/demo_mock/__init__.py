"""範例 extension：產生可重現的假資料。

存在的理由有三個：

1. 當成寫新 extension 時可以照抄的最小骨架；
2. 沒有網路或官方 API 掛掉時，本機仍能跑完整條 ingest 流程；
3. 測試用固定樣本（同一天同一品項永遠得到同一個價格）。

正式環境請在 `.env` 設 `EXTENSIONS_DISABLED=demo_mock` 關掉它。
"""

from app.extensions.demo_mock.source import DemoMockSource

# registry 認的就是這個名字：每個 extension 套件都必須匯出 SOURCE
SOURCE = DemoMockSource

__all__ = ["SOURCE", "DemoMockSource"]
