"""國家與行政區的參考資料。

這個 App 不綁定任何特定國家，所以這裡不維護自己的國家清單，
而是直接架在國際標準資料上：

| 資料 | 來源 | 涵蓋範圍 |
| --- | --- | --- |
| 國家代碼與名稱 | `pycountry`（ISO 3166-1） | 249 個 |
| 一級 / 二級行政區 | `pycountry`（ISO 3166-2） | 5046 個 |
| 在地化國名 | `babel`（CLDR） | 各語系 |
| 幣別 | `babel`（CLDR territory → ISO 4217） | 全部 |
| 國際電話碼與號碼驗證 | `phonenumbers`（libphonenumber） | 全部 |
| 時區 | `pytz`（IANA zone.tab） | 全部 |

原本這裡是一份手寫的 12 國字典，新增一個國家得手動補撥號碼、幣別、
時區與行政區清單——接烏干達時就卡在這裡。改用標準資料集之後，
**任何國家都是現成可用的，不需要改任何程式碼或資料**。

底下仍保留幾張小的覆蓋表，補標準資料沒有、或標準資料不適合直接對使用者
顯示的部分（見各表上的說明）。覆蓋是加法：沒有覆蓋就用標準資料。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache

import phonenumbers
import pycountry
import pytz
from babel import Locale, UnknownLocaleError
from babel.languages import get_official_languages
from babel.numbers import get_territory_currencies

from app.models.enums import UnitSystem

logger = logging.getLogger(__name__)

__all__ = [
    "COUNTRIES",
    "Country",
    "Subdivision",
    "country_codes",
    "default_currency",
    "default_locale",
    "default_timezone",
    "default_unit_system",
    "dialing_code",
    "get_country",
    "is_supported_country",
    "list_countries",
    "list_subdivisions",
    "lookup_subdivision",
    "normalize_subdivision_code",
    "subdivision_children",
]


# ---------------------------------------------------------------------------
# 覆蓋表
# ---------------------------------------------------------------------------

# 使用英制的國家。ISO / CLDR 都沒有這個資訊，但它會影響價格單位（lb vs kg），
# 對農產品 App 來說很實際。
_IMPERIAL_COUNTRIES = frozenset({"US", "LR", "MM"})

# 跨多時區的國家（31 個）要挑一個當預設。pytz 給的清單順序不保證是
# 「最多人用的那個」，這裡把幾個明確的指定掉，其餘取清單第一個。
_PRIMARY_TIMEZONE: dict[str, str] = {
    "US": "America/New_York",
    "CA": "America/Toronto",
    "AU": "Australia/Sydney",
    "BR": "America/Sao_Paulo",
    "RU": "Europe/Moscow",
    "CN": "Asia/Shanghai",
    "MX": "America/Mexico_City",
    "ID": "Asia/Jakarta",
    "AR": "America/Argentina/Buenos_Aires",
    "CD": "Africa/Kinshasa",
    "KZ": "Asia/Almaty",
    "ES": "Europe/Madrid",
    "PT": "Europe/Lisbon",
    "CL": "America/Santiago",
    "EC": "America/Guayaquil",
    "NZ": "Pacific/Auckland",
}

# 一級行政區在該國的通稱，給前端當表單標籤。
# 沒有覆蓋時會用 ISO 3166-2 的 type 欄位推出來（例如烏干達 → Region、
# 肯亞 → County），那個是英文的，所以中文語系國家才需要手動補。
_SUBDIVISION_LABELS: dict[str, str] = {
    "TW": "縣市",
    "JP": "都道府県",
    "CN": "省份",
    "HK": "地區",
    "KR": "시도",
}

# 少數 ISO type 直接拿來當表單標籤會很拗口，這裡換個說法。
_TYPE_LABEL_CLEANUP: dict[str, str] = {
    "Geographical region": "Region",
    "Geographical unit": "Region",
    "Geographical entity": "Region",
    "Special municipality": "Municipality",
    "Metropolitan department": "Department",
    "Autonomous republic": "Republic",
}

# 郵遞區號範例。沒有標準資料集，只給幾個常用的當表單提示；
# 沒有值就是 None，前端不顯示提示即可。不使用郵遞區號的國家也是 None。
_POSTAL_EXAMPLES: dict[str, str] = {
    "TW": "100", "JP": "100-0001", "KR": "03187", "CN": "100000",
    "US": "10001", "GB": "SW1A 1AA", "DE": "10115", "FR": "75001",
    "SG": "018956", "MY": "50000", "TH": "10200", "VN": "100000",
    "PH": "1000", "ID": "10110", "IN": "110001", "AU": "2000",
}

# CLDR 的「第一個官方語言」偶爾不是市場上實際通用的那個
# （例如烏干達的官方語言是斯瓦希里語與英語，但農產品行情實際上用英語）。
_DEFAULT_LOCALE: dict[str, str] = {
    "UG": "en",
    "KE": "en",
    "TZ": "sw",
    "IN": "en",
    "NG": "en",
    "ZA": "en",
    "PK": "en",
    "PH": "en",
    "SG": "en",
    "HK": "zh-Hant",
    "TW": "zh-Hant",
}

# ISO 3166-2 只給羅馬字名稱（TW-YUN → "Yunlin"、JP-13 → "Tokyo"）。
# 這裡補上當地語言的名稱；沒補到的就顯示 ISO 的羅馬字，不會出錯只是不好看。
_LOCAL_SUBDIVISION_NAMES: dict[str, dict[str, str]] = {}


def _register_local_names(pairs: list[tuple[str, str]], locale: str) -> None:
    for code, name in pairs:
        _LOCAL_SUBDIVISION_NAMES.setdefault(code, {})[locale] = name


_register_local_names([
    ("TW-CHA", "彰化縣"), ("TW-CYI", "嘉義市"), ("TW-CYQ", "嘉義縣"),
    ("TW-HSQ", "新竹縣"), ("TW-HSZ", "新竹市"), ("TW-HUA", "花蓮縣"),
    ("TW-ILA", "宜蘭縣"), ("TW-KEE", "基隆市"), ("TW-KHH", "高雄市"),
    ("TW-KIN", "金門縣"), ("TW-LIE", "連江縣"), ("TW-MIA", "苗栗縣"),
    ("TW-NAN", "南投縣"), ("TW-NWT", "新北市"), ("TW-PEN", "澎湖縣"),
    ("TW-PIF", "屏東縣"), ("TW-TAO", "桃園市"), ("TW-TNN", "臺南市"),
    ("TW-TPE", "臺北市"), ("TW-TTT", "臺東縣"), ("TW-TXG", "臺中市"),
    ("TW-YUN", "雲林縣"),
], "zh-Hant")

_register_local_names([
    ("JP-01", "北海道"), ("JP-02", "青森県"), ("JP-03", "岩手県"),
    ("JP-04", "宮城県"), ("JP-05", "秋田県"), ("JP-06", "山形県"),
    ("JP-07", "福島県"), ("JP-08", "茨城県"), ("JP-09", "栃木県"),
    ("JP-10", "群馬県"), ("JP-11", "埼玉県"), ("JP-12", "千葉県"),
    ("JP-13", "東京都"), ("JP-14", "神奈川県"), ("JP-15", "新潟県"),
    ("JP-16", "富山県"), ("JP-17", "石川県"), ("JP-18", "福井県"),
    ("JP-19", "山梨県"), ("JP-20", "長野県"), ("JP-21", "岐阜県"),
    ("JP-22", "静岡県"), ("JP-23", "愛知県"), ("JP-24", "三重県"),
    ("JP-25", "滋賀県"), ("JP-26", "京都府"), ("JP-27", "大阪府"),
    ("JP-28", "兵庫県"), ("JP-29", "奈良県"), ("JP-30", "和歌山県"),
    ("JP-31", "鳥取県"), ("JP-32", "島根県"), ("JP-33", "岡山県"),
    ("JP-34", "広島県"), ("JP-35", "山口県"), ("JP-36", "徳島県"),
    ("JP-37", "香川県"), ("JP-38", "愛媛県"), ("JP-39", "高知県"),
    ("JP-40", "福岡県"), ("JP-41", "佐賀県"), ("JP-42", "長崎県"),
    ("JP-43", "熊本県"), ("JP-44", "大分県"), ("JP-45", "宮崎県"),
    ("JP-46", "鹿児島県"), ("JP-47", "沖縄県"),
], "ja")


# ---------------------------------------------------------------------------
# 型別
# ---------------------------------------------------------------------------


def _match_locale(names: dict[str, str], locale: str) -> str | None:
    """先找完全相符的語系標籤，再退一階只比對語言。"""
    if locale in names:
        return names[locale]
    base = locale.split("-")[0].split("_")[0]
    for key, value in names.items():
        if key.split("-")[0].split("_")[0] == base:
            return value
    return None


@dataclass(frozen=True)
class Subdivision:
    """ISO 3166-2 的行政區。可能是第一層，也可能是某個第一層底下的第二層。"""

    code: str                      # 例如 TW-YUN、UG-E、UG-203
    name: str                      # ISO 的羅馬字名稱
    type: str                      # ISO 的類型，例如 County / District / Region
    country_code: str
    parent_code: str | None = None
    names: dict[str, str] = field(default_factory=dict)   # 當地語言名稱

    @property
    def level(self) -> int:
        return 1 if self.parent_code is None else 2

    def display_name(self, locale: str) -> str:
        return _match_locale(self.names, locale) or self.name


@dataclass(frozen=True)
class Country:
    code: str
    name_en: str
    dialing_code: str
    currency: str
    default_locale: str
    default_timezone: str
    unit_system: UnitSystem
    subdivision_label: str
    # 有第二層的國家（例如烏干達的 district）才有值。
    # 前端據此決定要不要顯示「再選下一層」的欄位。
    subdivision_label_level2: str | None
    postal_code_example: str | None
    subdivision_count: int
    subdivision_count_level2: int = 0

    @property
    def has_subdivision_data(self) -> bool:
        return self.subdivision_count > 0

    @property
    def has_second_level(self) -> bool:
        return self.subdivision_count_level2 > 0

    def display_name(self, locale: str) -> str:
        """用 CLDR 的在地化國名；查不到就退回英文。"""
        try:
            territories = Locale.parse(locale.replace("-", "_")).territories
        except (UnknownLocaleError, ValueError, TypeError):
            return self.name_en
        return territories.get(self.code) or self.name_en


# ---------------------------------------------------------------------------
# 建構
# ---------------------------------------------------------------------------


def _timezone_for(code: str) -> str:
    if code in _PRIMARY_TIMEZONE:
        return _PRIMARY_TIMEZONE[code]
    zones = pytz.country_timezones.get(code)
    # BV / HM 這種無人島沒有時區資料
    return zones[0] if zones else "UTC"


def _currency_for(code: str) -> str:
    try:
        currencies = get_territory_currencies(code)
    except Exception:  # pragma: no cover - CLDR 沒收錄的territory
        currencies = []
    return currencies[0] if currencies else "USD"


def _locale_for(code: str) -> str:
    if code in _DEFAULT_LOCALE:
        return _DEFAULT_LOCALE[code]
    try:
        langs = get_official_languages(code)
    except Exception:  # pragma: no cover
        langs = ()
    return langs[0].replace("_", "-") if langs else "en"


def _dialing_for(code: str) -> str | None:
    cc = phonenumbers.country_code_for_region(code)
    # libphonenumber 對沒有電話區碼的 territory 回 0
    return str(cc) if cc else None


def _label_from_types(subs: list[Subdivision]) -> str | None:
    """用該層最常見的 ISO type 當標籤，例如烏干達第二層 → District。"""
    if not subs:
        return None
    counts: dict[str, int] = {}
    for s in subs:
        counts[s.type] = counts.get(s.type, 0) + 1
    common = max(counts.items(), key=lambda kv: kv[1])[0]
    return _TYPE_LABEL_CLEANUP.get(common, common)


def _subdivision_label(code: str, tops: list[Subdivision]) -> str:
    """一級行政區的通稱。沒有覆蓋時用 ISO 最常見的 type。"""
    if code in _SUBDIVISION_LABELS:
        return _SUBDIVISION_LABELS[code]
    return _label_from_types(tops) or "Region"


@lru_cache(maxsize=None)
def _subdivisions_of(country_code: str) -> tuple[Subdivision, ...]:
    """某個國家的全部 ISO 3166-2 行政區（含第一層與第二層）。"""
    raw = pycountry.subdivisions.get(country_code=country_code) or []
    out = []
    for s in raw:
        out.append(
            Subdivision(
                code=s.code,
                name=s.name,
                type=s.type,
                country_code=country_code,
                parent_code=s.parent_code,
                names=dict(_LOCAL_SUBDIVISION_NAMES.get(s.code, {})),
            )
        )
    # 第一層在前，其次依代碼排序，讓下拉選單順序穩定
    out.sort(key=lambda s: (s.level, s.code))
    return tuple(out)


@lru_cache(maxsize=None)
def _build_country(code: str) -> Country | None:
    record = pycountry.countries.get(alpha_2=code)
    if record is None:
        return None
    dialing = _dialing_for(code)
    if dialing is None:
        # 沒有國際電話碼就沒辦法做 OTP 登入，這種 territory 直接不支援
        return None

    subs = _subdivisions_of(code)
    tops = [s for s in subs if s.level == 1]
    seconds = [s for s in subs if s.level == 2]
    return Country(
        code=code,
        # pycountry 的 common_name 比 name 適合顯示（例如 "Taiwan" vs
        # "Taiwan, Province of China"）；在地化名稱走 CLDR
        name_en=getattr(record, "common_name", None) or record.name,
        dialing_code=dialing,
        currency=_currency_for(code),
        default_locale=_locale_for(code),
        default_timezone=_timezone_for(code),
        unit_system=(
            UnitSystem.IMPERIAL if code in _IMPERIAL_COUNTRIES else UnitSystem.METRIC
        ),
        subdivision_label=_subdivision_label(code, tops),
        subdivision_label_level2=_label_from_types(seconds),
        postal_code_example=_POSTAL_EXAMPLES.get(code),
        subdivision_count=len(tops),
        subdivision_count_level2=len(seconds),
    )


class _CountryRegistry:
    """行為像 dict 的國家目錄，但內容是依需求從標準資料建出來的。

    保留 dict 介面是為了讓 `sorted(COUNTRIES)`、`code in COUNTRIES`
    這類既有寫法繼續可用。
    """

    def __getitem__(self, code: str) -> Country:
        country = _build_country(code.upper())
        if country is None:
            raise KeyError(code)
        return country

    def get(self, code: str, default: Country | None = None) -> Country | None:
        try:
            return self[code]
        except (KeyError, AttributeError):
            return default

    def __contains__(self, code: object) -> bool:
        return isinstance(code, str) and _build_country(code.upper()) is not None

    def __iter__(self):
        return iter(country_codes())

    def __len__(self) -> int:
        return len(country_codes())

    def values(self):
        return [self[c] for c in country_codes()]

    def items(self):
        return [(c, self[c]) for c in country_codes()]


COUNTRIES = _CountryRegistry()


@lru_cache(maxsize=1)
def country_codes() -> tuple[str, ...]:
    """所有支援的國家代碼（有國際電話碼的）。"""
    codes = [
        c.alpha_2 for c in pycountry.countries
        if _build_country(c.alpha_2) is not None
    ]
    return tuple(sorted(codes))


# ---------------------------------------------------------------------------
# 查詢
# ---------------------------------------------------------------------------


def get_country(code: str | None) -> Country | None:
    if not code:
        return None
    return _build_country(code.strip().upper())


def is_supported_country(code: str | None) -> bool:
    return get_country(code) is not None


def list_countries(locale: str = "en") -> list[Country]:
    """依在地化名稱排序，前端下拉選單直接用。"""
    countries = [_build_country(c) for c in country_codes()]
    resolved = [c for c in countries if c is not None]
    return sorted(resolved, key=lambda c: c.display_name(locale))


def list_subdivisions(
    country_code: str, *, parent: str | None = None, level: int | None = None
) -> list[Subdivision]:
    """列出行政區。

    預設只回第一層。烏干達這種「第一層是 4 個大區、實際要用的 district
    在第二層」的國家，前端可以用 `parent=UG-E` 往下鑽，
    或用 `level=2` 一次取得全部 district。
    """
    subs = _subdivisions_of(country_code.strip().upper())
    if parent:
        wanted = parent.strip().upper()
        return [s for s in subs if s.parent_code == wanted]
    if level is not None:
        return [s for s in subs if s.level == level]
    return [s for s in subs if s.level == 1]


def subdivision_children(code: str) -> list[Subdivision]:
    sub = lookup_subdivision(code)
    if sub is None:
        return []
    return list_subdivisions(sub.country_code, parent=sub.code)


def lookup_subdivision(code: str | None) -> Subdivision | None:
    if not code:
        return None
    normalized = code.strip().upper()
    country = normalized.split("-")[0]
    if len(country) != 2:
        return None
    for s in _subdivisions_of(country):
        if s.code == normalized:
            return s
    return None


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


def default_locale(country_code: str | None) -> str:
    country = get_country(country_code)
    return country.default_locale if country else "en"
