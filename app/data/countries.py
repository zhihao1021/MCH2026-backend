"""國家與一級行政區的參考資料。

這個 App 不限於單一國家使用，所以凡是「跟國家有關的預設值」都集中在這裡，
不散落在各處的字典裡：撥號碼（電話正規化用）、幣別（報價用）、
預設時區與語系、度量衡制（公制 / 英制），以及一級行政區清單。

行政區代碼一律使用 **ISO 3166-2**（例如 TW-TPE、JP-13、US-CA）。
用國際標準而不是自訂代碼，之後要跟其他系統對接才不會需要另一層對照表。

### 沒有行政區清單的國家怎麼辦

`SUBDIVISIONS` 目前只收了台灣與日本（有官方價格來源的兩個國家）。
其餘國家 `subdivisions` 會是空的，`has_subdivision_data` 為 False，
此時前端改用自由輸入的 `locality` 欄位即可——API 不會因此擋下註冊。

### 要新增一個國家

1. 在 `COUNTRIES` 加一筆 `Country`；
2. 若有一級行政區清單，在 `SUBDIVISIONS` 補上（代碼請照 ISO 3166-2）。

不需要改動任何程式邏輯。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from app.models.enums import UnitSystem

__all__ = [
    "COUNTRIES",
    "Country",
    "Subdivision",
    "default_currency",
    "default_timezone",
    "dialing_code",
    "get_country",
    "is_supported_country",
    "list_countries",
    "list_subdivisions",
    "lookup_subdivision",
    "normalize_subdivision_code",
]


@dataclass(frozen=True)
class Subdivision:
    """一級行政區（縣市 / 都道府県 / state / province）。"""

    # ISO 3166-2，含國碼前綴，例如 "TW-TPE"
    code: str
    name_en: str
    # 當地語言名稱：{locale: name}
    names: dict[str, str] = field(default_factory=dict)

    def display_name(self, locale: str) -> str:
        """取指定語系的名稱，沒有就退回英文。"""
        if locale in self.names:
            return self.names[locale]
        # zh-Hant-TW 這類帶地區的標籤，退一階再找
        base = locale.split("-")[0]
        for key, value in self.names.items():
            if key.split("-")[0] == base:
                return value
        return self.name_en


@dataclass(frozen=True)
class Country:
    code: str                    # ISO 3166-1 alpha-2
    name_en: str
    names: dict[str, str]        # {locale: name}
    dialing_code: str            # 不含 "+"
    currency: str                # ISO 4217
    default_locale: str
    default_timezone: str        # IANA；跨多時區的國家取主要那個
    unit_system: UnitSystem
    # 一級行政區在該國的稱呼，給前端當表單標籤用
    subdivision_label: str
    # 郵遞區號的提示格式；None 代表該國不使用郵遞區號
    postal_code_example: str | None = None

    def display_name(self, locale: str) -> str:
        if locale in self.names:
            return self.names[locale]
        base = locale.split("-")[0]
        for key, value in self.names.items():
            if key.split("-")[0] == base:
                return value
        return self.name_en


# ---------------------------------------------------------------------------
# 國家
# ---------------------------------------------------------------------------

COUNTRIES: dict[str, Country] = {
    c.code: c
    for c in [
        Country(
            code="TW", name_en="Taiwan",
            names={"zh-Hant": "臺灣", "ja": "台湾"},
            dialing_code="886", currency="TWD",
            default_locale="zh-Hant", default_timezone="Asia/Taipei",
            unit_system=UnitSystem.METRIC, subdivision_label="縣市",
            postal_code_example="100",
        ),
        Country(
            code="JP", name_en="Japan",
            names={"ja": "日本", "zh-Hant": "日本"},
            dialing_code="81", currency="JPY",
            default_locale="ja", default_timezone="Asia/Tokyo",
            unit_system=UnitSystem.METRIC, subdivision_label="都道府県",
            postal_code_example="100-0001",
        ),
        Country(
            code="KR", name_en="South Korea",
            names={"ko": "대한민국", "zh-Hant": "南韓"},
            dialing_code="82", currency="KRW",
            default_locale="ko", default_timezone="Asia/Seoul",
            unit_system=UnitSystem.METRIC, subdivision_label="시도",
            postal_code_example="03187",
        ),
        Country(
            code="CN", name_en="China",
            names={"zh-Hans": "中国", "zh-Hant": "中國"},
            dialing_code="86", currency="CNY",
            default_locale="zh-Hans", default_timezone="Asia/Shanghai",
            unit_system=UnitSystem.METRIC, subdivision_label="省份",
            postal_code_example="100000",
        ),
        Country(
            code="HK", name_en="Hong Kong",
            names={"zh-Hant": "香港"},
            dialing_code="852", currency="HKD",
            default_locale="zh-Hant", default_timezone="Asia/Hong_Kong",
            unit_system=UnitSystem.METRIC, subdivision_label="地區",
            postal_code_example=None,
        ),
        Country(
            code="SG", name_en="Singapore",
            names={"zh-Hant": "新加坡", "zh-Hans": "新加坡"},
            dialing_code="65", currency="SGD",
            default_locale="en", default_timezone="Asia/Singapore",
            unit_system=UnitSystem.METRIC, subdivision_label="District",
            postal_code_example="018956",
        ),
        Country(
            code="MY", name_en="Malaysia",
            names={"ms": "Malaysia", "zh-Hant": "馬來西亞"},
            dialing_code="60", currency="MYR",
            default_locale="ms", default_timezone="Asia/Kuala_Lumpur",
            unit_system=UnitSystem.METRIC, subdivision_label="Negeri",
            postal_code_example="50000",
        ),
        Country(
            code="TH", name_en="Thailand",
            names={"th": "ประเทศไทย", "zh-Hant": "泰國"},
            dialing_code="66", currency="THB",
            default_locale="th", default_timezone="Asia/Bangkok",
            unit_system=UnitSystem.METRIC, subdivision_label="จังหวัด",
            postal_code_example="10200",
        ),
        Country(
            code="VN", name_en="Vietnam",
            names={"vi": "Việt Nam", "zh-Hant": "越南"},
            dialing_code="84", currency="VND",
            default_locale="vi", default_timezone="Asia/Ho_Chi_Minh",
            unit_system=UnitSystem.METRIC, subdivision_label="Tỉnh",
            postal_code_example="100000",
        ),
        Country(
            code="PH", name_en="Philippines",
            names={"en": "Philippines", "zh-Hant": "菲律賓"},
            dialing_code="63", currency="PHP",
            default_locale="en", default_timezone="Asia/Manila",
            unit_system=UnitSystem.METRIC, subdivision_label="Province",
            postal_code_example="1000",
        ),
        Country(
            code="ID", name_en="Indonesia",
            names={"id": "Indonesia", "zh-Hant": "印尼"},
            dialing_code="62", currency="IDR",
            default_locale="id", default_timezone="Asia/Jakarta",
            unit_system=UnitSystem.METRIC, subdivision_label="Provinsi",
            postal_code_example="10110",
        ),
        Country(
            code="US", name_en="United States",
            names={"en": "United States", "zh-Hant": "美國"},
            dialing_code="1", currency="USD",
            default_locale="en", default_timezone="America/New_York",
            # 農產品在美國以磅計價，這是「不只台灣會用」最實際的一個差異
            unit_system=UnitSystem.IMPERIAL, subdivision_label="State",
            postal_code_example="10001",
        ),
    ]
}


# ---------------------------------------------------------------------------
# 一級行政區（ISO 3166-2）
# ---------------------------------------------------------------------------

_TW = [
    ("TW-CHA", "Changhua", "彰化縣"), ("TW-CYI", "Chiayi City", "嘉義市"),
    ("TW-CYQ", "Chiayi County", "嘉義縣"), ("TW-HSQ", "Hsinchu County", "新竹縣"),
    ("TW-HSZ", "Hsinchu City", "新竹市"), ("TW-HUA", "Hualien", "花蓮縣"),
    ("TW-ILA", "Yilan", "宜蘭縣"), ("TW-KEE", "Keelung", "基隆市"),
    ("TW-KHH", "Kaohsiung", "高雄市"), ("TW-KIN", "Kinmen", "金門縣"),
    ("TW-LIE", "Lienchiang", "連江縣"), ("TW-MIA", "Miaoli", "苗栗縣"),
    ("TW-NAN", "Nantou", "南投縣"), ("TW-NWT", "New Taipei", "新北市"),
    ("TW-PEN", "Penghu", "澎湖縣"), ("TW-PIF", "Pingtung", "屏東縣"),
    ("TW-TAO", "Taoyuan", "桃園市"), ("TW-TNN", "Tainan", "臺南市"),
    ("TW-TPE", "Taipei", "臺北市"), ("TW-TTT", "Taitung", "臺東縣"),
    ("TW-TXG", "Taichung", "臺中市"), ("TW-YUN", "Yunlin", "雲林縣"),
]

_JP = [
    ("JP-01", "Hokkaido", "北海道"), ("JP-02", "Aomori", "青森県"),
    ("JP-03", "Iwate", "岩手県"), ("JP-04", "Miyagi", "宮城県"),
    ("JP-05", "Akita", "秋田県"), ("JP-06", "Yamagata", "山形県"),
    ("JP-07", "Fukushima", "福島県"), ("JP-08", "Ibaraki", "茨城県"),
    ("JP-09", "Tochigi", "栃木県"), ("JP-10", "Gunma", "群馬県"),
    ("JP-11", "Saitama", "埼玉県"), ("JP-12", "Chiba", "千葉県"),
    ("JP-13", "Tokyo", "東京都"), ("JP-14", "Kanagawa", "神奈川県"),
    ("JP-15", "Niigata", "新潟県"), ("JP-16", "Toyama", "富山県"),
    ("JP-17", "Ishikawa", "石川県"), ("JP-18", "Fukui", "福井県"),
    ("JP-19", "Yamanashi", "山梨県"), ("JP-20", "Nagano", "長野県"),
    ("JP-21", "Gifu", "岐阜県"), ("JP-22", "Shizuoka", "静岡県"),
    ("JP-23", "Aichi", "愛知県"), ("JP-24", "Mie", "三重県"),
    ("JP-25", "Shiga", "滋賀県"), ("JP-26", "Kyoto", "京都府"),
    ("JP-27", "Osaka", "大阪府"), ("JP-28", "Hyogo", "兵庫県"),
    ("JP-29", "Nara", "奈良県"), ("JP-30", "Wakayama", "和歌山県"),
    ("JP-31", "Tottori", "鳥取県"), ("JP-32", "Shimane", "島根県"),
    ("JP-33", "Okayama", "岡山県"), ("JP-34", "Hiroshima", "広島県"),
    ("JP-35", "Yamaguchi", "山口県"), ("JP-36", "Tokushima", "徳島県"),
    ("JP-37", "Kagawa", "香川県"), ("JP-38", "Ehime", "愛媛県"),
    ("JP-39", "Kochi", "高知県"), ("JP-40", "Fukuoka", "福岡県"),
    ("JP-41", "Saga", "佐賀県"), ("JP-42", "Nagasaki", "長崎県"),
    ("JP-43", "Kumamoto", "熊本県"), ("JP-44", "Oita", "大分県"),
    ("JP-45", "Miyazaki", "宮崎県"), ("JP-46", "Kagoshima", "鹿児島県"),
    ("JP-47", "Okinawa", "沖縄県"),
]

SUBDIVISIONS: dict[str, list[Subdivision]] = {
    "TW": [Subdivision(code=c, name_en=en, names={"zh-Hant": zh}) for c, en, zh in _TW],
    "JP": [Subdivision(code=c, name_en=en, names={"ja": ja}) for c, en, ja in _JP],
}


# ---------------------------------------------------------------------------
# 查詢
# ---------------------------------------------------------------------------


def is_supported_country(code: str | None) -> bool:
    return bool(code) and code.upper() in COUNTRIES  # type: ignore[union-attr]


def get_country(code: str | None) -> Country | None:
    if not code:
        return None
    return COUNTRIES.get(code.upper())


def list_countries() -> list[Country]:
    """依英文名排序，前端下拉選單直接用。"""
    return sorted(COUNTRIES.values(), key=lambda c: c.name_en)


def list_subdivisions(country_code: str) -> list[Subdivision]:
    """沒有收錄的國家回空清單，呼叫端應改用自由輸入的 locality。"""
    return SUBDIVISIONS.get(country_code.upper(), [])


@lru_cache(maxsize=1)
def _subdivision_index() -> dict[str, Subdivision]:
    return {s.code: s for subs in SUBDIVISIONS.values() for s in subs}


def lookup_subdivision(code: str | None) -> Subdivision | None:
    if not code:
        return None
    return _subdivision_index().get(code.upper())


def normalize_subdivision_code(code: str | None) -> str | None:
    """統一成大寫。格式與歸屬的檢查由 schema 層負責。"""
    return code.strip().upper() if code and code.strip() else None


def dialing_code(country_code: str | None) -> str | None:
    country = get_country(country_code)
    return country.dialing_code if country else None


def default_currency(country_code: str | None) -> str:
    """查不到就用美元，讓報價至少能存下來而不是整個失敗。"""
    country = get_country(country_code)
    return country.currency if country else "USD"


def default_timezone(country_code: str | None) -> str:
    country = get_country(country_code)
    return country.default_timezone if country else "UTC"


def default_unit_system(country_code: str | None) -> UnitSystem:
    country = get_country(country_code)
    return country.unit_system if country else UnitSystem.METRIC
