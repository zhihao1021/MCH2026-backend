"""從 Wikipedia / Wikimedia Commons 幫每個品項抓一張圖，連同出處一起存。

    # 看會抓到什麼，不寫入
    python scripts/fetch_product_images.py

    # 確認後寫入（只補還沒有圖的）
    python scripts/fetch_product_images.py --apply

    # 連已經有圖的也重抓
    python scripts/fetch_product_images.py --apply --all

    # 只處理某幾個品項
    python scripts/fetch_product_images.py --apply --slug cabbage --slug pear

## 授權

Wikimedia 的圖多數是 CC BY-SA / CC BY，**授權要求標示來源、作者與條款**
（API 的 `AttributionRequired` 會明說）。所以這支腳本除了圖片網址，
還會一併存 `image_source` / `image_source_url` / `image_license` /
`image_author`——**顯示圖片的畫面就有義務顯示這些資訊**，
或至少提供一個連到出處頁的入口。只存網址不存出處是不合規的。

## 為什麼要人工對照表

自動用英文名查條目對了 98/134，其餘要嘛英文慣用名與條目名不同
（白蘿蔔 → Daikon）、要嘛得用學名（蓮霧 → Syzygium samarangense）、
要嘛條目沒有代表圖。`TITLE_OVERRIDES` 就是這份人工修正。

## 對 Wikimedia 有禮貌

- User-Agent 依他們的 robot policy 帶上專案與聯絡方式，否則會被 403
- 每次請求之間停一下，預設 0.5 秒
- 縮圖尺寸預設 320px：功能機螢幕寬 240px，再大只是浪費頻寬
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.database import session_scope
from app.models.catalog import Product

WIKI_API = "https://en.wikipedia.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# Wikimedia 的 robot policy 要求可辨識的 User-Agent 與聯絡方式，否則回 403
USER_AGENT = (
    "MCH2026-AgriPrice/0.1 "
    "(https://github.com/mch2026/agriprice; contact@chih-hao.xyz) python-httpx/0.28"
)

# slug -> 英文維基的條目名。只列自動查不到、或查到的圖不對的。
TITLE_OVERRIDES: dict[str, str] = {
    # 英文慣用名與條目名不同
    "white-radish": "Daikon",
    "green-onion": "Scallion",
    "napa-cabbage": "Napa cabbage",
    "large-cucumber": "Cucumber",
    "winter-melon": "Winter melon",
    "bamboo-shoot": "Bamboo shoot",
    "choy-sum": "Choy sum",
    "muskmelon": "Cantaloupe",
    "orange": "Orange (fruit)",
    "mixed-citrus": "Citrus",
    "egg": "Egg as food",
    # 要用學名才找得到
    "wax-apple": "Syzygium samarangense",
    "chinese-chive": "Allium tuberosum",
    "mustard-green": "Brassica juncea",
    "malabar-spinach": "Basella alba",
    "okinawa-spinach": "Gynura bicolor",
    "fern-vegetable": "Diplazium esculentum",
    "water-chestnut": "Eleocharis dulcis",
    "yam": "Dioscorea",
    "golden-fruit": "Pouteria caimito",
    "bird-of-paradise": "Strelitzia reginae",
    "renanthera": "Renanthera",
    "aranda": "Vanda",  # 千代蘭是萬代蘭屬的雜交種
    # 菇類一律用學名，通俗名常導到消歧義頁
    "button-mushroom": "Agaricus bisporus",
    "enoki-mushroom": "Enokitake",
    "king-oyster-mushroom": "Pleurotus eryngii",
    "shiitake-mushroom": "Shiitake",
    "shimeji-mushroom": "Shimeji",
    "wood-ear-mushroom": "Auricularia auricula-judae",
    "oyster-mushroom": "Pleurotus ostreatus",
    "other-mushroom": "Edible mushroom",
    # 來源本身就是分類桶，挑一個代表性的條目
    "sea-vegetable": "Edible seaweed",
    "sprouts": "Sprouting",
    "pickled-mustard": "Suan cai",
    "cut-foliage": "Foliage plant",
    "sweet-potato-leaf": "Sweet potato",
    # 條目沒有代表圖時，可以直接指定 Commons 檔案（以 "File:" 開頭）。
    # Asparagus 相關條目的首圖都是開花植株，對買菜的人沒有意義。
    "asparagus": "File:Asparagus-Bundle.jpg",
    # Coriander 條目的首圖是 1897 年的植物圖鑑插畫，且檔名含特殊字元
    # 會讓授權查詢失敗。改指定一張實照。
    "coriander": "File:Coriander.jpg",
}

# 這些品項刻意不配圖：來源的雜項桶，放任何一張圖都會誤導
SKIP: set[str] = set()


@dataclass
class ImageInfo:
    url: str
    source: str
    source_url: str
    license: str | None
    author: str | None
    title: str


def _strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", text).strip()


def _clean_thumb_url(url: str) -> str:
    """去掉 API 加上的 utm_* 追蹤參數，存乾淨的網址。"""
    return url.split("?")[0]


class WikiClient:
    def __init__(self, thumb_size: int, delay: float) -> None:
        self.client = httpx.Client(
            timeout=30.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )
        self.thumb_size = thumb_size
        self.delay = delay

    def close(self) -> None:
        self.client.close()

    def _sleep(self) -> None:
        if self.delay > 0:
            import time

            time.sleep(self.delay)

    def lookup(self, title: str) -> ImageInfo | None:
        if title.startswith("File:"):
            page = self._commons_file(title)
        else:
            page = self._page_image(title)
        if page is None:
            return None
        thumb, filename, resolved_title = page
        meta = self._file_metadata(filename)
        return ImageInfo(
            url=_clean_thumb_url(thumb),
            source="Wikimedia Commons",
            source_url=meta.get("descriptionurl")
            or f"https://commons.wikimedia.org/wiki/File:{filename}",
            license=meta.get("license"),
            author=meta.get("author"),
            title=resolved_title,
        )

    def _page_image(self, title: str) -> tuple[str, str, str] | None:
        try:
            r = self.client.get(
                WIKI_API,
                params={
                    "action": "query",
                    "format": "json",
                    "prop": "pageimages",
                    "piprop": "thumbnail|name",
                    "pithumbsize": self.thumb_size,
                    "titles": title,
                    "redirects": 1,
                },
            )
            self._sleep()
            r.raise_for_status()
            pages = r.json().get("query", {}).get("pages", {})
        except (httpx.HTTPError, ValueError):
            return None

        for pid, page in pages.items():
            if pid == "-1":
                return None
            thumb = (page.get("thumbnail") or {}).get("source")
            filename = page.get("pageimage")
            if thumb and filename:
                return thumb, filename, page.get("title", title)
        return None

    def _commons_file(self, file_title: str) -> tuple[str, str, str] | None:
        """直接取 Commons 上某個檔案的縮圖。給條目沒有代表圖的品項用。"""
        try:
            r = self.client.get(
                COMMONS_API,
                params={
                    "action": "query",
                    "format": "json",
                    "prop": "imageinfo",
                    "iiprop": "url",
                    "iiurlwidth": self.thumb_size,
                    "titles": file_title,
                },
            )
            self._sleep()
            r.raise_for_status()
            pages = r.json().get("query", {}).get("pages", {})
        except (httpx.HTTPError, ValueError):
            return None

        for pid, page in pages.items():
            if pid == "-1":
                return None
            info = (page.get("imageinfo") or [{}])[0]
            thumb = info.get("thumburl")
            if thumb:
                filename = file_title.removeprefix("File:")
                return thumb, filename, file_title
        return None

    def _file_metadata(self, filename: str) -> dict[str, str]:
        try:
            r = self.client.get(
                COMMONS_API,
                params={
                    "action": "query",
                    "format": "json",
                    "prop": "imageinfo",
                    "iiprop": "extmetadata|url",
                    "titles": f"File:{filename}",
                },
            )
            self._sleep()
            r.raise_for_status()
            pages = r.json().get("query", {}).get("pages", {})
        except (httpx.HTTPError, ValueError):
            return {}

        for page in pages.values():
            info = (page.get("imageinfo") or [{}])[0]
            em = info.get("extmetadata") or {}

            def get(key: str) -> str | None:
                raw = (em.get(key) or {}).get("value")
                return _strip_html(str(raw))[:200] if raw else None

            return {
                "descriptionurl": info.get("descriptionurl", ""),
                "license": (get("LicenseShortName") or "")[:80] or None,
                "author": get("Artist"),
            }
        return {}


async def run(
    apply: bool, refetch_all: bool, slugs: list[str], thumb_size: int, delay: float
) -> int:
    async with session_scope() as session:
        stmt = select(Product).options(selectinload(Product.names)).order_by(Product.slug)
        if slugs:
            stmt = stmt.where(Product.slug.in_(slugs))
        elif not refetch_all:
            stmt = stmt.where(Product.image_url.is_(None))
        products = list((await session.execute(stmt)).scalars())

        if not products:
            print("沒有需要處理的品項（已經都有圖了？加 --all 可重抓）")
            return 0

        print(f"要處理 {len(products)} 個品項，縮圖 {thumb_size}px\n")
        wiki = WikiClient(thumb_size, delay)
        found = skipped = failed = 0
        failures: list[tuple[str, str]] = []

        try:
            for product in products:
                if product.slug in SKIP:
                    skipped += 1
                    continue

                title = TITLE_OVERRIDES.get(product.slug) or _english_name(product)
                if not title:
                    failed += 1
                    failures.append((product.slug, "沒有英文名，也沒有人工指定條目"))
                    continue

                info = wiki.lookup(title)
                if info is None:
                    failed += 1
                    failures.append((product.slug, f"查無圖片（條目 {title!r}）"))
                    continue

                found += 1
                zh = product.display_name("zh-Hant")
                print(f"  {product.slug:22} {zh:10} <- {info.title}")
                print(f"{'':24} {info.license or '授權不明'} / {info.author or '作者不明'}")

                if apply:
                    product.image_url = info.url
                    product.image_source = info.source
                    product.image_source_url = info.source_url
                    product.image_license = info.license
                    product.image_author = info.author
        finally:
            wiki.close()

        print(f"\n找到 {found}，跳過 {skipped}，失敗 {failed}")
        if failures:
            print("\n失敗的（請補進 TITLE_OVERRIDES）：")
            for slug, why in failures:
                print(f"  {slug:22} {why}")

        if not apply:
            print("\n（dry-run，未寫入。加 --apply 才會實際寫入）")
        else:
            print(f"\n已寫入 {found} 個品項的圖片與出處")
    return 0


def _english_name(product: Product) -> str | None:
    for n in product.names:
        if n.locale == "en" and n.is_primary:
            return n.name
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--apply", action="store_true", help="實際寫入（預設只試算）")
    parser.add_argument("--all", dest="refetch_all", action="store_true", help="連已有圖的也重抓")
    parser.add_argument("--slug", action="append", default=[], help="只處理指定的 slug，可重複")
    parser.add_argument(
        "--thumb-size", type=int, default=320, help="縮圖寬度 px（功能機螢幕寬 240，預設 320）"
    )
    parser.add_argument("--delay", type=float, default=0.5, help="每次請求間隔秒數")
    args = parser.parse_args()
    return asyncio.run(
        run(args.apply, args.refetch_all, args.slug, args.thumb_size, args.delay)
    )


if __name__ == "__main__":
    sys.exit(main())
