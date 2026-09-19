"""建立平台的標準品項清單。

    python scripts/seed_products.py [--reset-popularity]

可重複執行：已存在的 slug 會跳過，只補上缺少的名稱與別名。

## 收錄範圍

**只收農作物**：蔬菜、水果、花卉、穀物。

畜產（雞蛋、肉品）與漁產不在這個 App 的範圍內——它們的產銷結構與
交易單位跟作物差很多（計價單位、分級、交易所），硬塞進同一套品項模型
只會讓對照與單位換算變得沒辦法維護。`ProductCategory` 仍保留
`livestock` / `fishery` 兩個值，將來真的要做時不必改 schema。

花卉留著：它是園藝作物，也在 MOA 的農產品交易行情裡，
`popularity` 壓低避免洗版即可。

## 命名原則

- `zh-Hant` 的主要名稱用一般人講的說法（高麗菜），別名放官方/學名/俗名
  （甘藍、包心菜）。**別名就是搜尋詞，也是自動對照的依據**，
  所以 MOA 用「甘藍」、日本用「キャベツ」都能落到同一個品項。
- `ja` / `en` 只在確定正確時才填。填錯比不填糟——這些名稱會被搜尋和
  跨國對照拿去用。例如「柚子」在日文是另一種水果（ゆず），
  所以文旦只給英文 Pomelo，不給日文。
- 品種層級不另開品項。MOA 的 OT百合 / LA百合 / 鐵砲百合 / 香水百合
  都是百合的品種，一律當作 `lily` 的別名，靠 `product_source_mappings`
  多對一收斂。

## 熱門度

`popularity` 決定清單排序。日常食材給高分，花卉刻意壓低，
免得功能機首頁被 400 多個花卉代碼洗版。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.database import session_scope
from app.models.catalog import Product, ProductName
from app.models.enums import ProductCategory

V = ProductCategory.VEGETABLE
F = ProductCategory.FRUIT
L = ProductCategory.FLOWER
G = ProductCategory.GRAIN

# (slug, 分類, 單位, 熱門度, [(locale, 名稱, 是否為主要名稱)])
SEED: list[tuple[str, ProductCategory, str, int, list[tuple[str, str, bool]]]] = [
    # ---- 核心蔬菜 ----
    ("cabbage", V, "kg", 100, [
        ("zh-Hant", "高麗菜", True), ("zh-Hant", "甘藍", False), ("zh-Hant", "包心菜", False),
        ("ja", "キャベツ", True), ("en", "Cabbage", True),
    ]),
    ("napa-cabbage", V, "kg", 85, [
        ("zh-Hant", "大白菜", True), ("zh-Hant", "結球白菜", False),
        ("zh-Hant", "包心白菜", False), ("zh-Hant", "包心白", False),
        ("ja", "白菜", True), ("en", "Napa Cabbage", True),
    ]),
    ("white-radish", V, "kg", 80, [
        ("zh-Hant", "白蘿蔔", True), ("zh-Hant", "菜頭", False), ("zh-Hant", "蘿蔔", False),
        ("ja", "大根", True), ("en", "Daikon Radish", True),
    ]),
    ("tomato", V, "kg", 95, [
        ("zh-Hant", "番茄", True), ("zh-Hant", "牛番茄", False), ("zh-Hant", "西紅柿", False),
        ("ja", "トマト", True), ("en", "Tomato", True), ("en", "Tomatoes", False),
    ]),
    ("spinach", V, "kg", 70, [
        ("zh-Hant", "菠菜", True), ("ja", "ほうれん草", True), ("en", "Spinach", True),
    ]),
    ("carrot", V, "kg", 78, [
        ("zh-Hant", "紅蘿蔔", True), ("zh-Hant", "胡蘿蔔", False),
        ("ja", "にんじん", True), ("en", "Carrot", True),
    ]),
    ("onion", V, "kg", 88, [
        ("zh-Hant", "洋蔥", True), ("ja", "玉ねぎ", True), ("en", "Onion", True),
    ]),
    ("potato", V, "kg", 82, [
        ("zh-Hant", "馬鈴薯", True), ("zh-Hant", "洋芋", False),
        ("ja", "じゃがいも", True), ("en", "Potato", True),
        # 東非慣稱 Irish Potato，與 sweet potato 區分
        ("en", "Irish Potato", False), ("en", "Potatoes - Irish", False),
    ]),
    ("cucumber", V, "kg", 72, [
        ("zh-Hant", "小黃瓜", True), ("zh-Hant", "花胡瓜", False),
        ("ja", "きゅうり", True), ("en", "Cucumber", True),
    ]),
    ("green-onion", V, "kg", 90, [
        ("zh-Hant", "青蔥", True), ("zh-Hant", "蔥", False),
        ("ja", "ねぎ", True), ("en", "Green Onion", True),
    ]),
    # ---- 核心水果 ----
    ("banana", F, "kg", 92, [
        ("zh-Hant", "香蕉", True), ("zh-Hant", "芎蕉", False),
        ("ja", "バナナ", True), ("en", "Banana", True),
        # 烏干達的鮮食蕉品種名。Matooke 是煮食蕉，另外開品項
        ("en", "Apple Bananas", False), ("en", "Cavendish (Bogoya)", False),
    ]),
    ("pineapple", F, "kg", 86, [
        ("zh-Hant", "鳳梨", True), ("zh-Hant", "菠蘿", False),
        ("ja", "パイナップル", True), ("en", "Pineapple", True),
        ("en", "Pineapples", False),
    ]),
    ("mango", F, "kg", 84, [
        ("zh-Hant", "芒果", True), ("zh-Hant", "檨仔", False),
        ("ja", "マンゴー", True), ("en", "Mango", True),
    ]),
    ("watermelon", F, "kg", 76, [
        ("zh-Hant", "西瓜", True), ("ja", "スイカ", True), ("en", "Watermelon", True),
        ("en", "Water melon", False),
    ]),
    ("guava", F, "kg", 74, [
        ("zh-Hant", "芭樂", True), ("zh-Hant", "番石榴", False),
        ("ja", "グアバ", True), ("en", "Guava", True),
    ]),
    ("wax-apple", F, "kg", 68, [
        ("zh-Hant", "蓮霧", True), ("ja", "レンブ", True), ("en", "Wax Apple", True),
    ]),
    ("papaya", F, "kg", 66, [
        ("zh-Hant", "木瓜", True), ("ja", "パパイヤ", True), ("en", "Papaya", True),
    ]),
    ("apple", F, "kg", 80, [
        ("zh-Hant", "蘋果", True), ("ja", "りんご", True), ("en", "Apple", True),
    ]),
    # ---- 其他 ----
    ("rice", G, "kg", 94, [
        ("zh-Hant", "白米", True), ("zh-Hant", "稻米", False),
        ("ja", "米", True), ("en", "Rice", True),
    ]),

    # =======================================================================
    # 以下依 MOA 實際交易量補進來的品項
    # =======================================================================

    # ---- 瓜果菜類 ----
    ("pumpkin", V, "kg", 64, [
        ("zh-Hant", "南瓜", True), ("ja", "かぼちゃ", True), ("en", "Pumpkin", True),
        ("en", "Pumpkins", False),
    ]),
    ("sweet-corn", V, "kg", 64, [
        ("zh-Hant", "玉米", True), ("ja", "とうもろこし", True), ("en", "Corn", True),
    ]),
    ("bell-pepper", V, "kg", 60, [
        ("zh-Hant", "甜椒", True), ("ja", "パプリカ", True), ("en", "Bell Pepper", True),
    ]),
    ("chili-pepper", V, "kg", 60, [
        ("zh-Hant", "辣椒", True), ("ja", "唐辛子", True), ("en", "Chili Pepper", True),
    ]),
    ("bitter-gourd", V, "kg", 58, [
        ("zh-Hant", "苦瓜", True), ("ja", "ゴーヤ", True), ("en", "Bitter Gourd", True),
    ]),
    ("luffa", V, "kg", 58, [
        ("zh-Hant", "絲瓜", True), ("ja", "へちま", True), ("en", "Luffa", True),
    ]),
    ("eggplant", V, "kg", 56, [
        ("zh-Hant", "茄子", True), ("ja", "なす", True), ("en", "Eggplant", True),
    ]),
    ("bottle-gourd", V, "kg", 52, [
        ("zh-Hant", "扁蒲", True), ("zh-Hant", "瓠瓜", False),
        ("ja", "ゆうがお", True), ("en", "Bottle Gourd", True),
    ]),
    ("winter-melon", V, "kg", 50, [
        ("zh-Hant", "冬瓜", True), ("ja", "とうがん", True), ("en", "Winter Melon", True),
    ]),
    ("chayote", V, "kg", 48, [
        ("zh-Hant", "隼人瓜", True), ("zh-Hant", "佛手瓜", False),
        ("ja", "はやとうり", True), ("en", "Chayote", True),
    ]),
    ("large-cucumber", V, "kg", 50, [
        # MOA 的「胡瓜」指大黃瓜，與小黃瓜（花胡瓜）是不同的交易品項
        ("zh-Hant", "大黃瓜", True), ("zh-Hant", "胡瓜", False),
        ("en", "Cucumber (Large)", True),
    ]),
    ("okra", V, "kg", 40, [
        ("zh-Hant", "黃秋葵", True), ("zh-Hant", "秋葵", False),
        ("ja", "オクラ", True), ("en", "Okra", True),
    ]),

    # ---- 葉菜類 ----
    ("lettuce", V, "kg", 62, [
        ("zh-Hant", "萵苣菜", True), ("zh-Hant", "萵苣", False), ("zh-Hant", "美生菜", False),
        ("ja", "レタス", True), ("en", "Lettuce", True),
    ]),
    ("pak-choi", V, "kg", 58, [
        ("zh-Hant", "小白菜", True), ("en", "Pak Choi", True),
    ]),
    ("bok-choy", V, "kg", 54, [
        ("zh-Hant", "青江白菜", True), ("zh-Hant", "青江菜", False),
        ("ja", "チンゲン菜", True), ("en", "Bok Choy", True),
    ]),
    ("water-spinach", V, "kg", 54, [
        ("zh-Hant", "蕹菜", True), ("zh-Hant", "空心菜", False),
        ("en", "Water Spinach", True),
    ]),
    ("amaranth", V, "kg", 46, [
        ("zh-Hant", "莧菜", True), ("en", "Amaranth", True),
    ]),
    ("chinese-kale", V, "kg", 46, [
        ("zh-Hant", "芥藍菜", True), ("zh-Hant", "芥藍", False), ("en", "Chinese Kale", True),
    ]),
    ("choy-sum", V, "kg", 44, [
        ("zh-Hant", "油菜", True), ("en", "Choy Sum", True),
    ]),
    ("mustard-green", V, "kg", 44, [
        ("zh-Hant", "芥菜", True), ("ja", "からし菜", True), ("en", "Mustard Green", True),
    ]),
    ("celery", V, "kg", 48, [
        ("zh-Hant", "芹菜", True), ("ja", "セロリ", True), ("en", "Celery", True),
    ]),
    ("chinese-chive", V, "kg", 50, [
        ("zh-Hant", "韭菜", True), ("ja", "にら", True), ("en", "Chinese Chive", True),
    ]),
    ("cauliflower", V, "kg", 52, [
        ("zh-Hant", "花椰菜", True), ("ja", "カリフラワー", True), ("en", "Cauliflower", True),
    ]),
    ("sweet-potato-leaf", V, "kg", 44, [
        ("zh-Hant", "甘薯葉", True), ("zh-Hant", "地瓜葉", False),
    ]),
    ("malabar-spinach", V, "kg", 36, [
        ("zh-Hant", "皇宮菜", True),
    ]),
    ("coriander", V, "kg", 40, [
        ("zh-Hant", "芫荽", True), ("zh-Hant", "香菜", False),
        ("ja", "パクチー", True), ("en", "Coriander", True),
    ]),
    ("thai-basil", V, "kg", 38, [
        ("zh-Hant", "九層塔", True), ("en", "Thai Basil", True),
    ]),
    ("okinawa-spinach", V, "kg", 34, [
        ("zh-Hant", "紅鳳菜", True),
    ]),

    # ---- 根莖 / 豆類 ----
    ("sweet-potato", V, "kg", 62, [
        ("zh-Hant", "甘薯", True), ("zh-Hant", "地瓜", False),
        ("ja", "さつまいも", True), ("en", "Sweet Potato", True),
        ("en", "Potatoes - Sweet White", False), ("en", "Potatoes - Sweet Red", False),
    ]),
    ("taro", V, "kg", 52, [
        ("zh-Hant", "芋", True), ("zh-Hant", "芋頭", False),
        ("ja", "さといも", True), ("en", "Taro", True),
    ]),
    ("yam", V, "kg", 44, [
        ("zh-Hant", "薯蕷", True), ("zh-Hant", "山藥", False),
        ("ja", "やまいも", True), ("en", "Yam", True),
    ]),
    ("garlic", V, "kg", 62, [
        ("zh-Hant", "大蒜", True), ("zh-Hant", "蒜頭", False),
        ("ja", "にんにく", True), ("en", "Garlic", True),
    ]),
    ("ginger", V, "kg", 56, [
        ("zh-Hant", "薑", True), ("zh-Hant", "生薑", False),
        ("ja", "しょうが", True), ("en", "Ginger", True),
    ]),
    ("bamboo-shoot", V, "kg", 52, [
        ("zh-Hant", "竹筍", True), ("ja", "たけのこ", True), ("en", "Bamboo Shoot", True),
    ]),
    ("water-bamboo", V, "kg", 46, [
        ("zh-Hant", "茭白筍", True), ("en", "Water Bamboo", True),
    ]),
    ("lotus-root", V, "kg", 38, [
        ("zh-Hant", "蓮藕", True), ("ja", "れんこん", True), ("en", "Lotus Root", True),
    ]),
    ("burdock", V, "kg", 36, [
        ("zh-Hant", "牛蒡", True), ("ja", "ごぼう", True), ("en", "Burdock", True),
    ]),
    ("water-chestnut", V, "kg", 30, [
        ("zh-Hant", "荸薺", True), ("en", "Water Chestnut", True),
    ]),
    ("jicama", V, "kg", 32, [
        ("zh-Hant", "豆薯", True), ("en", "Jicama", True),
    ]),
    ("asparagus", V, "kg", 44, [
        ("zh-Hant", "蘆筍", True), ("ja", "アスパラガス", True), ("en", "Asparagus", True),
    ]),
    ("green-bean", V, "kg", 46, [
        ("zh-Hant", "敏豆", True), ("zh-Hant", "四季豆", False),
        ("ja", "いんげん", True), ("en", "Green Bean", True),
    ]),
    ("yardlong-bean", V, "kg", 42, [
        ("zh-Hant", "菜豆", True), ("en", "Yardlong Bean", True),
    ]),
    ("pea", V, "kg", 42, [
        ("zh-Hant", "豌豆", True), ("ja", "えんどう", True), ("en", "Pea", True),
    ]),
    ("edamame", V, "kg", 34, [
        ("zh-Hant", "毛豆", True), ("ja", "枝豆", True), ("en", "Edamame", True),
    ]),
    ("peanut", V, "kg", 34, [
        ("zh-Hant", "落花生", True), ("zh-Hant", "花生", False),
        ("ja", "落花生", True), ("en", "Peanut", True),
        # 非洲與英式英文慣稱 groundnut
        ("en", "Groundnuts", False), ("en", "Groundnut", False),
    ]),
    ("sprouts", V, "kg", 36, [
        ("zh-Hant", "芽菜類", True), ("zh-Hant", "芽菜", False), ("en", "Sprouts", True),
    ]),

    # ---- 菇類 ----
    ("king-oyster-mushroom", V, "kg", 42, [
        ("zh-Hant", "杏鮑菇", True), ("ja", "エリンギ", True), ("en", "King Oyster Mushroom", True),
    ]),
    ("enoki-mushroom", V, "kg", 42, [
        ("zh-Hant", "金絲菇", True), ("zh-Hant", "金針菇", False),
        ("ja", "えのき", True), ("en", "Enoki Mushroom", True),
    ]),
    ("shimeji-mushroom", V, "kg", 38, [
        ("zh-Hant", "鴻喜菇", True), ("ja", "しめじ", True), ("en", "Shimeji Mushroom", True),
    ]),
    ("shiitake-mushroom", V, "kg", 40, [
        ("zh-Hant", "濕香菇", True), ("zh-Hant", "香菇", False),
        ("ja", "しいたけ", True), ("en", "Shiitake Mushroom", True),
    ]),
    ("wood-ear-mushroom", V, "kg", 36, [
        ("zh-Hant", "濕木耳", True), ("zh-Hant", "木耳", False),
        ("ja", "きくらげ", True), ("en", "Wood Ear Mushroom", True),
    ]),
    ("button-mushroom", V, "kg", 34, [
        ("zh-Hant", "洋菇", True), ("ja", "マッシュルーム", True), ("en", "Button Mushroom", True),
    ]),
    ("oyster-mushroom", V, "kg", 32, [
        ("zh-Hant", "秀珍菇", True), ("ja", "ひらたけ", True), ("en", "Oyster Mushroom", True),
    ]),

    # ---- 水果 ----
    ("pear", F, "kg", 70, [
        ("zh-Hant", "梨", True), ("zh-Hant", "水梨", False),
        ("ja", "梨", True), ("en", "Pear", True),
    ]),
    ("dragon-fruit", F, "kg", 66, [
        ("zh-Hant", "紅龍果", True), ("zh-Hant", "火龍果", False),
        ("ja", "ドラゴンフルーツ", True), ("en", "Dragon Fruit", True),
    ]),
    ("persimmon", F, "kg", 60, [
        ("zh-Hant", "柿子", True), ("ja", "柿", True), ("en", "Persimmon", True),
    ]),
    ("grape", F, "kg", 64, [
        ("zh-Hant", "葡萄", True), ("ja", "ぶどう", True), ("en", "Grape", True),
    ]),
    ("longan", F, "kg", 56, [
        ("zh-Hant", "龍眼", True), ("ja", "リュウガン", True), ("en", "Longan", True),
    ]),
    ("passion-fruit", F, "kg", 56, [
        ("zh-Hant", "百香果", True), ("ja", "パッションフルーツ", True),
        ("en", "Passion Fruit", True), ("en", "Passion fruits", False),
    ]),
    ("muskmelon", F, "kg", 60, [
        ("zh-Hant", "洋香瓜", True), ("zh-Hant", "香瓜", False),
        ("ja", "メロン", True), ("en", "Muskmelon", True),
    ]),
    ("melon", F, "kg", 50, [
        ("zh-Hant", "甜瓜", True), ("en", "Melon", True),
    ]),
    ("cherry-tomato", F, "kg", 62, [
        # MOA 把小番茄歸在水果類（N05），與蔬菜的牛番茄分開計價
        ("zh-Hant", "小番茄", True), ("zh-Hant", "聖女番茄", False),
        ("ja", "ミニトマト", True), ("en", "Cherry Tomato", True),
    ]),
    ("peach", F, "kg", 52, [
        ("zh-Hant", "桃子", True), ("ja", "桃", True), ("en", "Peach", True),
    ]),
    ("plum", F, "kg", 44, [
        ("zh-Hant", "李", True), ("zh-Hant", "李子", False),
        ("ja", "すもも", True), ("en", "Plum", True),
    ]),
    ("pomelo", F, "kg", 58, [
        # 日文的「柚子」是另一種水果（ゆず），所以這裡不給日文名
        ("zh-Hant", "柚子", True), ("zh-Hant", "文旦", False), ("en", "Pomelo", True),
    ]),
    ("mandarin", F, "kg", 54, [
        ("zh-Hant", "柑橘", True), ("ja", "みかん", True), ("en", "Mandarin Orange", True),
    ]),
    ("mixed-citrus", F, "kg", 50, [
        ("zh-Hant", "雜柑", True),
    ]),
    ("orange", F, "kg", 48, [
        ("zh-Hant", "甜橙", True), ("zh-Hant", "柳橙", False),
        ("ja", "オレンジ", True), ("en", "Orange", True),
    ]),
    ("grapefruit", F, "kg", 38, [
        ("zh-Hant", "葡萄柚", True), ("ja", "グレープフルーツ", True), ("en", "Grapefruit", True),
    ]),
    ("kiwifruit", F, "kg", 46, [
        ("zh-Hant", "奇異果", True), ("ja", "キウイ", True), ("en", "Kiwifruit", True),
    ]),
    ("blueberry", F, "kg", 40, [
        ("zh-Hant", "藍莓", True), ("ja", "ブルーベリー", True), ("en", "Blueberry", True),
    ]),
    ("cherry", F, "kg", 40, [
        ("zh-Hant", "櫻桃", True), ("ja", "さくらんぼ", True), ("en", "Cherry", True),
    ]),
    ("starfruit", F, "kg", 38, [
        ("zh-Hant", "楊桃", True), ("en", "Starfruit", True),
    ]),
    ("sugar-apple", F, "kg", 48, [
        ("zh-Hant", "釋迦", True), ("en", "Sugar Apple", True),
    ]),
    ("durian", F, "kg", 36, [
        ("zh-Hant", "榴槤", True), ("ja", "ドリアン", True), ("en", "Durian", True),
    ]),
    ("mangosteen", F, "kg", 32, [
        ("zh-Hant", "山竹", True), ("ja", "マンゴスチン", True), ("en", "Mangosteen", True),
    ]),
    ("rambutan", F, "kg", 32, [
        ("zh-Hant", "紅毛丹", True), ("ja", "ランブータン", True), ("en", "Rambutan", True),
    ]),
    ("coconut", F, "kg", 46, [
        ("zh-Hant", "椰子", True), ("ja", "ココナッツ", True), ("en", "Coconut", True),
    ]),
    ("strawberry", F, "kg", 54, [
        ("zh-Hant", "草莓", True), ("ja", "いちご", True), ("en", "Strawberry", True),
    ]),
    ("golden-fruit", F, "kg", 28, [
        ("zh-Hant", "黃金果", True),
    ]),
    ("avocado", F, "kg", 50, [
        ("zh-Hant", "酪梨", True), ("ja", "アボカド", True), ("en", "Avocado", True),
    ]),
    ("cempedak", F, "kg", 26, [
        ("zh-Hant", "榴槤蜜", True), ("en", "Cempedak", True),
    ]),

    # ---- 補上的蔬菜 ----
    ("broccoli", V, "kg", 56, [
        # MOA 用「青花苔」這個寫法，底下還有青花筍（broccolini）
        ("zh-Hant", "青花菜", True), ("zh-Hant", "青花苔", False),
        ("zh-Hant", "綠花椰菜", False),
        ("ja", "ブロッコリー", True), ("en", "Broccoli", True),
    ]),
    ("sea-vegetable", V, "kg", 38, [
        # MOA 把水蓮與海帶都歸在「海菜」底下，這裡沿用來源的分組
        ("zh-Hant", "海菜", True), ("en", "Sea Vegetable", True),
    ]),
    ("fern-vegetable", V, "kg", 34, [
        # 過貓、山蘇都在這個基底名底下
        ("zh-Hant", "蕨菜", True), ("en", "Fern Vegetable", True),
    ]),
    ("lemongrass", V, "kg", 30, [
        ("zh-Hant", "香茅", True), ("en", "Lemongrass", True),
    ]),
    ("pickled-mustard", V, "kg", 28, [
        ("zh-Hant", "鹹菜", True), ("en", "Pickled Mustard", True),
    ]),
    ("other-mushroom", V, "kg", 26, [
        ("zh-Hant", "其他菇類", True), ("en", "Other Mushroom", True),
    ]),

    # ---- 花卉（熱門度刻意壓低，免得洗版首頁）----
    ("rose", L, "stem", 18, [
        ("zh-Hant", "玫瑰", True), ("zh-Hant", "進口玫瑰", False),
        ("ja", "バラ", True), ("en", "Rose", True),
    ]),
    ("lisianthus", L, "stem", 16, [
        ("zh-Hant", "洋桔梗", True), ("ja", "トルコキキョウ", True), ("en", "Lisianthus", True),
    ]),
    ("anthurium", L, "stem", 16, [
        ("zh-Hant", "火鶴花", True), ("ja", "アンスリウム", True), ("en", "Anthurium", True),
    ]),
    ("chrysanthemum", L, "stem", 16, [
        # MOA 把菊花依大小、產地、染色拆成很多代碼，這裡全部收斂成一個品項
        ("zh-Hant", "菊花", True),
        ("zh-Hant", "小菊", False), ("zh-Hant", "大菊", False),
        ("zh-Hant", "進口小菊", False), ("zh-Hant", "進口大菊", False),
        ("zh-Hant", "染小菊", False), ("zh-Hant", "染大菊", False),
        ("zh-Hant", "進口染色大菊", False),
        ("ja", "菊", True), ("en", "Chrysanthemum", True),
    ]),
    ("lily", L, "stem", 15, [
        # OT / LA / 鐵砲 都是百合的品種分類，不另開品項
        ("zh-Hant", "百合", True),
        ("zh-Hant", "香水百合", False), ("zh-Hant", "OT百合", False),
        ("zh-Hant", "LA百合", False), ("zh-Hant", "鐵砲百合", False),
        ("zh-Hant", "重瓣百合", False), ("zh-Hant", "水晶香水", False),
        ("zh-Hant", "OT大連", False), ("zh-Hant", "OT曼尼薩", False),
        ("zh-Hant", "OT紅福特", False), ("zh-Hant", "OT試金石", False),
        ("zh-Hant", "OT帕雷諾", False),
        ("ja", "ゆり", True), ("en", "Lily", True),
    ]),
    ("gerbera", L, "stem", 14, [
        ("zh-Hant", "非洲菊", True), ("ja", "ガーベラ", True), ("en", "Gerbera", True),
    ]),
    ("carnation", L, "stem", 13, [
        ("zh-Hant", "康乃馨", True), ("zh-Hant", "進口康乃馨", False),
        ("ja", "カーネーション", True), ("en", "Carnation", True),
    ]),
    ("gladiolus", L, "stem", 12, [
        ("zh-Hant", "劍蘭", True), ("ja", "グラジオラス", True), ("en", "Gladiolus", True),
    ]),
    ("hydrangea", L, "stem", 12, [
        ("zh-Hant", "繡球花", True), ("ja", "あじさい", True), ("en", "Hydrangea", True),
    ]),
    ("phalaenopsis", L, "stem", 12, [
        ("zh-Hant", "蝴蝶蘭", True), ("ja", "胡蝶蘭", True), ("en", "Phalaenopsis", True),
    ]),
    ("oncidium", L, "stem", 11, [
        ("zh-Hant", "文心蘭", True), ("zh-Hant", "檸檬綠文心蘭", False),
        ("ja", "オンシジューム", True), ("en", "Oncidium", True),
    ]),
    ("dendrobium", L, "stem", 11, [
        ("zh-Hant", "石斛蘭", True), ("zh-Hant", "進口石斛蘭", False),
        ("ja", "デンドロビウム", True), ("en", "Dendrobium", True),
    ]),
    ("vanda", L, "stem", 10, [
        ("zh-Hant", "萬代蘭", True), ("ja", "バンダ", True), ("en", "Vanda", True),
    ]),
    ("cockscomb", L, "stem", 10, [
        ("zh-Hant", "雞冠花", True), ("ja", "ケイトウ", True), ("en", "Cockscomb", True),
    ]),
    ("sunflower", L, "stem", 12, [
        ("zh-Hant", "向日葵", True), ("ja", "ひまわり", True), ("en", "Sunflower", True),
    ]),
    ("dahlia", L, "stem", 10, [
        ("zh-Hant", "大理花", True), ("ja", "ダリア", True), ("en", "Dahlia", True),
    ]),
    ("alstroemeria", L, "stem", 10, [
        ("zh-Hant", "水仙百合", True), ("zh-Hant", "進口水仙百合", False),
        ("ja", "アルストロメリア", True), ("en", "Alstroemeria", True),
    ]),
    ("bird-of-paradise", L, "stem", 9, [
        ("zh-Hant", "天堂鳥", True), ("zh-Hant", "小天堂鳥", False),
        ("ja", "ストレリチア", True), ("en", "Bird of Paradise", True),
    ]),
    ("babys-breath", L, "stem", 9, [
        ("zh-Hant", "滿天星", True), ("ja", "カスミソウ", True), ("en", "Baby's Breath", True),
    ]),
    ("eucalyptus", L, "stem", 8, [
        ("zh-Hant", "尤加利葉", True), ("ja", "ユーカリ", True), ("en", "Eucalyptus", True),
    ]),
    ("limonium", L, "stem", 8, [
        ("zh-Hant", "卡斯比亞", True), ("en", "Limonium", True),
    ]),
    ("solidago", L, "stem", 8, [
        ("zh-Hant", "麒麟草", True), ("en", "Solidago", True),
    ]),
    ("curcuma", L, "stem", 7, [
        ("zh-Hant", "薑荷花", True), ("en", "Curcuma", True),
    ]),
    ("aranda", L, "stem", 7, [
        ("zh-Hant", "千代蘭", True),
    ]),
    ("renanthera", L, "stem", 7, [
        ("zh-Hant", "腎藥蘭", True),
    ]),
    ("cut-foliage", L, "stem", 6, [
        # MOA 的各種切葉分開計價，但對消費者來說是同一類
        ("zh-Hant", "切葉類", True),
        ("zh-Hant", "百合竹", False), ("zh-Hant", "蓮花竹", False),
        ("zh-Hant", "白竹", False), ("zh-Hant", "水燭葉", False),
        ("zh-Hant", "電信蘭葉", False), ("zh-Hant", "黃椰心葉", False),
        ("zh-Hant", "八角金盤", False),
        ("en", "Cut Foliage", True),
    ]),
    # =======================================================================
    # 東非主食作物
    #
    # 接烏干達 NAMIS 時補進來的。這些在台灣市場沒有交易，但對非洲的
    # 使用者是主食；品項清單是全球共用的，不該只長台灣看得到的東西。
    #
    # 注意：`maize` 與既有的 `sweet-corn` 是不同的東西——前者是曬乾的
    # 粒玉米（主食、磨粉），後者是鮮食甜玉米。所以 maize 的中文主要名稱
    # 刻意避開「玉米」，免得兩個品項在搜尋與自動對照上互相干擾。
    #
    # 穀物磨成粉之後是另一個價格層級（NAMIS 分開報），所以也分開建品項。
    # =======================================================================
    ("maize", G, "kg", 70, [
        ("zh-Hant", "粒玉米", True), ("zh-Hant", "飼料玉米", False),
        ("zh-Hant", "玉米粒", False),
        ("en", "Maize", True), ("en", "Maize Grain", False),
    ]),
    ("maize-flour", G, "kg", 68, [
        ("zh-Hant", "玉米粉", True),
        ("en", "Maize Flour", True), ("en", "Posho", False),
    ]),
    ("millet", G, "kg", 58, [
        ("zh-Hant", "小米", True), ("zh-Hant", "粟", False),
        ("ja", "きび", True),
        ("en", "Millet", True), ("en", "Millet Grain", False),
        ("en", "Finger Millet", False),
    ]),
    ("millet-flour", G, "kg", 56, [
        ("zh-Hant", "小米粉", True), ("en", "Millet Flour", True),
    ]),
    ("sorghum", G, "kg", 56, [
        ("zh-Hant", "高粱", True), ("ja", "ソルガム", True),
        ("en", "Sorghum", True), ("en", "Sorghum Grain", False),
    ]),
    ("sorghum-flour", G, "kg", 54, [
        ("zh-Hant", "高粱粉", True), ("en", "Sorghum Flour", True),
    ]),
    ("cassava", V, "kg", 66, [
        ("zh-Hant", "木薯", True), ("zh-Hant", "樹薯", False),
        ("ja", "キャッサバ", True),
        ("en", "Cassava", True), ("en", "Cassava - Fresh", False),
    ]),
    ("cassava-flour", G, "kg", 60, [
        ("zh-Hant", "木薯粉", True), ("zh-Hant", "樹薯粉", False),
        ("en", "Cassava Flour", True), ("en", "Cassava - Flour", False),
    ]),
    ("sesame", G, "kg", 52, [
        ("zh-Hant", "芝麻", True), ("zh-Hant", "胡麻", False),
        ("ja", "ごま", True),
        # Simsim 是東非對芝麻的稱呼
        ("en", "Sesame", True), ("en", "Simsim", False),
    ]),
    ("cooking-banana", F, "kg", 64, [
        ("zh-Hant", "煮食蕉", True), ("zh-Hant", "大蕉", False),
        ("ja", "料理用バナナ", True),
        # Matooke 是東非高地煮食蕉，與鮮食的 banana 是不同商品
        ("en", "Cooking Banana", True), ("en", "Matooke", False),
        # NAMIS 用 "Matooke (kg)" 表示每公斤價；另一筆同名的是每串價，
        # 單位在來源就標錯了，所以列在 automap 的排除清單裡
        ("en", "Matooke (kg)", False), ("en", "Plantain", False),
    ]),
    # 曬乾的菜豆種子（Phaseolus vulgaris），與既有的 green-bean（鮮食豆莢）
    # 和 yardlong-bean（豇豆，台灣稱菜豆）都是不同的交易品項。
    # 中文主要名稱刻意用「乾豆」而不是「菜豆」，避免與 yardlong-bean 撞名。
    # 日文名稱不填：不確定對應哪一個詞，填錯比不填糟。
    ("common-bean", V, "kg", 62, [
        ("zh-Hant", "乾豆", True), ("zh-Hant", "乾燥豆類", False),
        ("zh-Hant", "腰豆", False),
        ("en", "Common Bean", True), ("en", "Beans", False),
        ("en", "Dry Beans", False), ("en", "Kidney Bean", False),
    ]),
    ("cowpea", V, "kg", 50, [
        ("zh-Hant", "豇豆", True), ("ja", "ささげ", True),
        ("en", "Cowpea", True), ("en", "Cow Peas", False),
    ]),
    ("soybean", V, "kg", 58, [
        ("zh-Hant", "大豆", True), ("zh-Hant", "黃豆", False),
        ("ja", "大豆", True),
        ("en", "Soybean", True), ("en", "Soya", False),
        ("en", "Beans - Soya", False),
    ]),
]


async def seed(reset_popularity: bool) -> tuple[int, int, int]:
    created = updated = names_added = 0

    async with session_scope() as session:
        for slug, category, unit, popularity, names in SEED:
            product = await session.scalar(
                select(Product).options(selectinload(Product.names)).where(Product.slug == slug)
            )

            if product is None:
                # 名稱直接掛在關聯上一起建，不要先 flush 再讀 product.names——
                # 那會在新物件上觸發 lazy load，非同步 session 下會炸 MissingGreenlet
                product = Product(
                    slug=slug,
                    category=category,
                    default_unit=unit,
                    popularity=popularity,
                    names=[
                        ProductName(locale=locale, name=name, is_primary=is_primary)
                        for locale, name, is_primary in names
                    ],
                )
                session.add(product)
                created += 1
                names_added += len(names)
                continue

            if reset_popularity and product.popularity != popularity:
                product.popularity = popularity
                updated += 1

            # 既有品項是用 selectinload 讀進來的，這裡存取 names 不會再打 DB
            existing = {(n.locale, n.name) for n in product.names}
            for locale, name, is_primary in names:
                if (locale, name) in existing:
                    continue
                product.names.append(
                    ProductName(locale=locale, name=name, is_primary=is_primary)
                )
                names_added += 1

    return created, updated, names_added


def check_duplicates() -> list[str]:
    """同一個名稱出現在兩個品項上會讓自動對照無法判斷，建之前先擋下來。"""
    seen: dict[tuple[str, str], str] = {}
    problems: list[str] = []
    slugs: set[str] = set()
    for slug, _, _, _, names in SEED:
        if slug in slugs:
            problems.append(f"slug 重複：{slug}")
        slugs.add(slug)
        for locale, name, _ in names:
            key = (locale, name)
            if key in seen and seen[key] != slug:
                problems.append(f"名稱重複：{locale} {name!r} 同時出現在 {seen[key]} 與 {slug}")
            seen[key] = slug
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset-popularity", action="store_true", help="把既有品項的熱門度覆寫回種子值"
    )
    args = parser.parse_args()

    problems = check_duplicates()
    if problems:
        print("種子資料有問題，未寫入：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2

    created, updated, names = asyncio.run(seed(args.reset_popularity))
    print(f"新增品項 {created} 筆，更新 {updated} 筆，補上名稱 / 別名 {names} 筆")
    print(f"種子清單共 {len(SEED)} 個品項")
    return 0


if __name__ == "__main__":
    sys.exit(main())
