"""烏干達 NAMIS（National Agro-Market Information Services）extension。

資料來自 https://www.nmis.infotradeconnect.com/index?tab=index
（Uganda Warehouse Receipt System Authority 與 INFOTRADE 合作營運）。

該站**沒有 API、沒有 CSV 匯出、也沒有歷史查詢**，只有一張伺服器端算好的
HTML 表格，呈現「最新一次」的各市場批發價（W.P）與零售價（R.P）。
因此這個 extension 是網頁解析，而且只能抓當天：錯過就補不回來，
所以排程刻意設成一天跑好幾次（見 source.py 的說明）。

設定（都是選填）：

    EXTENSIONS_CONFIG={"ug_namis":{
        "markets": ["owino", "mbale"],        # 只抓這些市場，留空 = 全部
        "include_retail": true,               # 要不要一併收零售價
        "exclude_categories": ["Fish"]        # 覆寫預設排除的非作物類別
    }}
"""

from app.extensions.ug_namis.source import UgNamisSource

# registry 認的就是這個名字：每個 extension 套件都必須匯出 SOURCE
SOURCE = UgNamisSource

__all__ = ["SOURCE", "UgNamisSource"]
