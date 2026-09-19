"""個人檔案與位置的測試。

重點放在「這個 App 不只台灣會用」會踩到的地方：
行政區代碼的歸屬、沒有行政區清單的國家、地址排列順序、
以及位置隱私的揭露程度。需要資料庫的部分由 smoke_test 涵蓋。
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.data.countries import (
    COUNTRIES,
    country_codes,
    default_currency,
    default_locale,
    default_timezone,
    default_unit_system,
    dialing_code,
    get_country,
    list_countries,
    list_subdivisions,
    lookup_subdivision,
    subdivision_children,
)
from app.models.enums import LocationVisibility, UnitSystem, UserRole
from app.models.user import User
from app.schemas.profile import (
    SUBDIVISION_PATTERN,
    CountryOut,
    LocationIn,
    LocationOut,
    ProfileUpdate,
    PublicLocationOut,
    PublicUserOut,
    SubdivisionOut,
    effective_currency,
    effective_unit_system,
    format_address,
)


def make_user(**kwargs) -> User:
    """建一個不進資料庫的 User。created_at 要自己給，那是 server_default。"""
    defaults = dict(
        # id 平常由 server_default 產生，transient 物件要自己給
        id=uuid.uuid4(),
        phone="+886912345678",
        role=UserRole.FARMER,
        country_code="TW",
        locale="zh-Hant",
        location_visibility=LocationVisibility.REGION,
        contact_phone_public=True,
        is_active=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    defaults.update(kwargs)
    return User(**defaults)


# ---------------------------------------------------------------------------
# 國家資料本身
#
# 資料來自 pycountry / babel / phonenumbers / pytz，不是手寫清單，
# 所以這裡測的是「接線有沒有接對」與「涵蓋範圍夠不夠」，
# 而不是逐筆核對內容。
# ---------------------------------------------------------------------------

# 一個 ISO 3166-2 沒有收錄行政區的國家（小島居多），用來測 fallback 路徑
NO_SUBDIVISION_COUNTRY = "AW"   # 阿魯巴


def test_covers_essentially_every_country() -> None:
    """不是只有少數幾國——接烏干達之前這裡只有 12 個。"""
    assert len(country_codes()) > 200
    for code in ["UG", "TW", "JP", "US", "KE", "TZ", "NG", "IN", "BR", "FR"]:
        assert code in COUNTRIES, code


def test_every_country_has_consistent_code() -> None:
    for code in country_codes():
        assert re.fullmatch(r"[A-Z]{2}", code), code
        assert COUNTRIES[code].code == code


def test_every_country_has_usable_defaults() -> None:
    """每一國都要能算出撥號碼、幣別、時區與度量衡，否則註冊流程會卡住。"""
    for code in country_codes():
        c = COUNTRIES[code]
        assert c.dialing_code.isdigit(), (code, c.dialing_code)
        assert re.fullmatch(r"[A-Z]{3}", c.currency), (code, c.currency)
        assert "/" in c.default_timezone or c.default_timezone == "UTC", code
        assert isinstance(c.unit_system, UnitSystem), code
        assert c.subdivision_label, code


def test_uganda_is_fully_supported() -> None:
    ug = get_country("UG")
    assert ug is not None
    assert ug.dialing_code == "256"
    assert ug.currency == "UGX"
    assert ug.default_timezone == "Africa/Kampala"
    assert ug.unit_system is UnitSystem.METRIC
    assert ug.display_name("zh-Hant") == "烏干達"
    assert ug.display_name("ja") == "ウガンダ"


def test_subdivision_codes_are_well_formed() -> None:
    for code in ["UG", "TW", "JP", "KE", "US"]:
        for sub in list_subdivisions(code, level=1) + list_subdivisions(code, level=2):
            assert SUBDIVISION_PATTERN.match(sub.code), sub.code
            assert sub.code.startswith(f"{code}-"), sub.code
            assert sub.name and sub.type


def test_subdivision_counts() -> None:
    assert len(list_subdivisions("TW")) == 22   # 6 直轄市 + 13 縣 + 3 市
    assert len(list_subdivisions("JP")) == 47   # 都道府県
    assert len(list_subdivisions("KE")) == 47   # counties


def test_uganda_needs_the_second_level() -> None:
    """烏干達的一級只有 4 個大區，實際要用的 district 在第二層。

    這正是把行政區從「單層清單」改成「階層」的原因。
    """
    ug = get_country("UG")
    regions = list_subdivisions("UG")
    assert len(regions) == 4
    assert {r.code for r in regions} == {"UG-C", "UG-E", "UG-N", "UG-W"}
    assert all(r.level == 1 for r in regions)

    districts = list_subdivisions("UG", level=2)
    assert len(districts) > 100
    assert all(d.level == 2 and d.parent_code for d in districts)

    assert ug.has_second_level is True
    assert ug.subdivision_label == "Region"
    assert ug.subdivision_label_level2 == "District"


def test_can_drill_down_from_a_region() -> None:
    eastern = subdivision_children("UG-E")
    assert eastern
    assert all(s.parent_code == "UG-E" for s in eastern)
    assert "Iganga" in {s.name for s in eastern}


def test_single_level_countries_report_no_second_level() -> None:
    for code in ["TW", "JP", "KE", "US"]:
        c = get_country(code)
        assert c.has_second_level is False, code
        assert list_subdivisions(code, level=2) == [], code


def test_country_without_iso_subdivisions() -> None:
    """ISO 沒收錄的國家回空清單，前端改用自由輸入的 locality。"""
    assert list_subdivisions(NO_SUBDIVISION_COUNTRY) == []
    assert get_country(NO_SUBDIVISION_COUNTRY).has_subdivision_data is False
    assert list_subdivisions("ZZ") == []


def test_lookup_subdivision_is_case_insensitive() -> None:
    assert lookup_subdivision("tw-yun") is not None
    assert lookup_subdivision("TW-YUN").name == "Yunlin"
    assert lookup_subdivision("ug-203").name == "Iganga"
    assert lookup_subdivision("TW-ZZZ") is None
    assert lookup_subdivision(None) is None


def test_country_lookup_helpers() -> None:
    assert dialing_code("UG") == "256"
    assert dialing_code("tw") == "886"
    assert dialing_code("ZZ") is None
    assert default_currency("UG") == "UGX"
    assert default_currency("JP") == "JPY"
    assert default_currency("ZZ") == "USD"      # 查不到退回美元而不是炸掉
    assert default_timezone("JP") == "Asia/Tokyo"
    assert default_timezone("ZZ") == "UTC"
    assert default_locale("UG") == "en"


def test_multi_timezone_countries_get_a_sensible_primary() -> None:
    """美國有 29 個時區，不能隨便抓一個。"""
    assert default_timezone("US") == "America/New_York"
    assert default_timezone("BR") == "America/Sao_Paulo"
    assert default_timezone("AU") == "Australia/Sydney"


def test_us_uses_imperial_everyone_else_metric() -> None:
    """農產品在美國以磅計價，這是不只服務台灣最實際的一個差異。"""
    assert default_unit_system("US") is UnitSystem.IMPERIAL
    assert default_unit_system("TW") is UnitSystem.METRIC
    assert default_unit_system("UG") is UnitSystem.METRIC


def test_display_name_falls_back_to_english() -> None:
    tw = COUNTRIES["TW"]
    assert tw.display_name("zh-Hant") == "台灣"
    assert tw.display_name("zh-Hant-TW") == "台灣"   # 帶地區的標籤要退一階
    assert tw.display_name("xx-broken") == tw.name_en
    assert COUNTRIES["UG"].display_name("de") == "Uganda"


def test_local_subdivision_names_override_iso_romanisation() -> None:
    """ISO 只給羅馬字，中日文名稱靠覆蓋表補。"""
    assert lookup_subdivision("TW-YUN").display_name("zh-Hant") == "雲林縣"
    assert lookup_subdivision("JP-13").display_name("ja") == "東京都"
    # 沒補到的就顯示 ISO 名稱，不會出錯
    assert lookup_subdivision("UG-203").display_name("zh-Hant") == "Iganga"


def test_countries_are_sorted_by_localised_name() -> None:
    names = [c.display_name("en") for c in list_countries("en")]
    assert names == sorted(names)


# ---------------------------------------------------------------------------
# LocationIn 驗證
# ---------------------------------------------------------------------------


def test_minimal_location_only_needs_country() -> None:
    loc = LocationIn(country_code="tw")
    assert loc.country_code == "TW"
    assert loc.visibility is LocationVisibility.REGION


def test_unsupported_country_is_rejected_with_guidance() -> None:
    with pytest.raises(ValidationError) as exc:
        LocationIn(country_code="ZZ")
    assert "geo/countries" in str(exc.value)


def test_subdivision_must_belong_to_the_country() -> None:
    with pytest.raises(ValidationError) as exc:
        LocationIn(country_code="JP", subdivision_code="TW-YUN")
    assert "不屬於國家" in str(exc.value)


def test_subdivision_must_exist_in_the_list() -> None:
    with pytest.raises(ValidationError) as exc:
        LocationIn(country_code="TW", subdivision_code="TW-ZZZ")
    assert "不在" in str(exc.value)


def test_subdivision_rejected_when_iso_has_no_data() -> None:
    """ISO 沒收錄行政區的國家，不該讓人塞一個沒人驗證得了的代碼進來。"""
    with pytest.raises(ValidationError) as exc:
        LocationIn(country_code=NO_SUBDIVISION_COUNTRY,
                   subdivision_code=f"{NO_SUBDIVISION_COUNTRY}-XX")
    assert "locality" in str(exc.value)


def test_country_without_iso_data_can_still_register_with_locality() -> None:
    loc = LocationIn(country_code=NO_SUBDIVISION_COUNTRY, locality="Oranjestad")
    assert loc.locality == "Oranjestad"
    assert loc.subdivision_code is None


def test_us_states_are_now_accepted() -> None:
    """改用 ISO 3166-2 之後，美國的州別不再需要手工維護。"""
    loc = LocationIn(country_code="US", subdivision_code="US-CA")
    assert loc.subdivision_code == "US-CA"
    assert lookup_subdivision("US-CA").name == "California"


def test_uganda_district_is_accepted() -> None:
    """第二層的 district 也要收，不能只准填 4 個大區。"""
    loc = LocationIn(country_code="UG", subdivision_code="ug-203", locality="Iganga Town")
    assert loc.subdivision_code == "UG-203"


def test_uganda_region_is_also_accepted() -> None:
    assert LocationIn(country_code="UG", subdivision_code="UG-E").subdivision_code == "UG-E"


def test_malformed_subdivision_code_is_rejected() -> None:
    with pytest.raises(ValidationError):
        LocationIn(country_code="TW", subdivision_code="雲林")


def test_subdivision_code_is_upper_cased() -> None:
    assert LocationIn(country_code="TW", subdivision_code="tw-yun").subdivision_code == "TW-YUN"


def test_coordinates_must_come_in_pairs() -> None:
    with pytest.raises(ValidationError) as exc:
        LocationIn(country_code="TW", latitude=23.7)
    assert "成對" in str(exc.value)
    # 兩個都給就沒問題
    LocationIn(country_code="TW", latitude=23.7, longitude=120.5)


def test_coordinates_are_range_checked() -> None:
    with pytest.raises(ValidationError):
        LocationIn(country_code="TW", latitude=91, longitude=0)
    with pytest.raises(ValidationError):
        LocationIn(country_code="TW", latitude=0, longitude=181)


def test_timezone_must_be_a_real_iana_zone() -> None:
    assert LocationIn(country_code="JP", timezone="Asia/Tokyo").timezone == "Asia/Tokyo"
    with pytest.raises(ValidationError):
        LocationIn(country_code="JP", timezone="Mars/Olympus")


def test_blank_strings_become_none() -> None:
    loc = LocationIn(country_code="TW", locality="   ", postal_code="")
    assert loc.locality is None
    assert loc.postal_code is None


def test_location_in_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        LocationIn(country_code="TW", region="雲林縣")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# 位置的公開程度
# ---------------------------------------------------------------------------


@pytest.fixture
def located_user() -> User:
    return make_user(
        subdivision_code="TW-YUN",
        locality="西螺鎮",
        address_line="延平路 100 號",
        postal_code="648",
        latitude=23.797512,
        longitude=120.465843,
    )


def test_exact_visibility_shows_everything(located_user: User) -> None:
    located_user.location_visibility = LocationVisibility.EXACT
    out = PublicLocationOut.from_model(located_user, "zh-Hant")
    assert out.address_line == "延平路 100 號"
    assert out.latitude == 23.797512
    assert out.precision == "exact"
    assert out.subdivision_name == "雲林縣"


def test_approximate_visibility_fuzzes_coordinates(located_user: User) -> None:
    located_user.location_visibility = LocationVisibility.APPROXIMATE
    out = PublicLocationOut.from_model(located_user, "zh-Hant")
    assert out.address_line is None          # 地址不給
    assert out.latitude == 23.8              # 約 1 公里精度
    assert out.longitude == 120.47
    assert out.precision == "approximate_1km"
    assert out.locality == "西螺鎮"


def test_region_visibility_is_the_default_and_hides_coordinates(located_user: User) -> None:
    out = PublicLocationOut.from_model(located_user, "zh-Hant")
    assert located_user.location_visibility is LocationVisibility.REGION
    assert out.subdivision_name == "雲林縣"
    assert out.locality == "西螺鎮"
    assert out.address_line is None
    assert out.latitude is None
    assert out.precision == "hidden"


def test_private_visibility_shows_country_only(located_user: User) -> None:
    located_user.location_visibility = LocationVisibility.PRIVATE
    out = PublicLocationOut.from_model(located_user, "zh-Hant")
    assert out.country_name == "台灣"
    assert out.subdivision_code is None
    assert out.locality is None
    assert out.latitude is None
    assert out.formatted == "台灣"


def test_owner_view_always_shows_everything(located_user: User) -> None:
    """本人看自己的資料不受 visibility 影響，否則沒辦法確認自己填了什麼。"""
    located_user.location_visibility = LocationVisibility.PRIVATE
    out = LocationOut.from_model(located_user, "zh-Hant")
    assert out.address_line == "延平路 100 號"
    assert out.latitude == 23.797512
    assert out.postal_code == "648"


# ---------------------------------------------------------------------------
# 地址排列順序
# ---------------------------------------------------------------------------


def test_cjk_addresses_are_formatted_big_to_small() -> None:
    user = make_user(subdivision_code="TW-YUN", locality="西螺鎮", address_line="延平路 100 號")
    assert format_address(user, "zh-Hant") == "台灣 雲林縣 西螺鎮"
    assert format_address(user, "zh-Hant", full=True) == "台灣 雲林縣 西螺鎮 延平路 100 號"


def test_japanese_addresses_are_also_big_to_small() -> None:
    user = make_user(
        country_code="JP", locale="ja", subdivision_code="JP-13", locality="世田谷区"
    )
    assert format_address(user, "ja") == "日本 東京都 世田谷区"


def test_western_addresses_are_formatted_small_to_big() -> None:
    user = make_user(country_code="US", locale="en", locality="Fresno", address_line="1 Main St")
    assert format_address(user, "en", full=True) == "1 Main St, Fresno, United States"


def test_postal_code_position_differs_by_country() -> None:
    tw = make_user(subdivision_code="TW-YUN", locality="西螺鎮", postal_code="648")
    assert format_address(tw, "zh-Hant", full=True).startswith("648 ")

    us = make_user(country_code="US", locale="en", locality="Fresno", postal_code="93721")
    formatted = format_address(us, "en", full=True)
    assert formatted.endswith("93721, United States"), formatted


def test_format_address_handles_missing_pieces() -> None:
    assert format_address(make_user(), "zh-Hant") == "台灣"


# ---------------------------------------------------------------------------
# ProfileUpdate
# ---------------------------------------------------------------------------


def test_profile_update_rejects_role() -> None:
    """身分綁定後不能自己改，送 role 要明確報錯而不是被默默忽略。"""
    with pytest.raises(ValidationError):
        ProfileUpdate(display_name="阿明", role="trader")  # type: ignore[call-arg]


def test_profile_update_rejects_location_fields() -> None:
    """位置走專屬端點，混在個人檔案裡改會繞過驗證。"""
    with pytest.raises(ValidationError):
        ProfileUpdate(locality="西螺鎮")  # type: ignore[call-arg]


def test_profile_update_partial_only_reports_sent_fields() -> None:
    p = ProfileUpdate(business_name="阿明果園")
    assert p.model_dump(exclude_unset=True) == {"business_name": "阿明果園"}


def test_profile_update_explicit_null_clears_a_field() -> None:
    """明確送 null 代表清空，要跟「沒送」區分開。"""
    p = ProfileUpdate(bio=None)
    assert p.model_dump(exclude_unset=True) == {"bio": None}


def test_currency_is_upper_cased_and_validated() -> None:
    assert ProfileUpdate(preferred_currency="jpy").preferred_currency == "JPY"
    with pytest.raises(ValidationError):
        ProfileUpdate(preferred_currency="12")


def test_urls_must_be_http() -> None:
    assert ProfileUpdate(website_url="https://farm.example").website_url == "https://farm.example"
    with pytest.raises(ValidationError):
        ProfileUpdate(website_url="javascript:alert(1)")
    with pytest.raises(ValidationError):
        ProfileUpdate(avatar_url="ftp://example.org/a.png")


# ---------------------------------------------------------------------------
# 偏好設定的解析
# ---------------------------------------------------------------------------


def test_preferences_follow_country_when_unset() -> None:
    assert effective_currency(make_user(country_code="JP")) == "JPY"
    assert effective_currency(make_user(country_code="UG")) == "UGX"
    assert effective_unit_system(make_user(country_code="US")) is UnitSystem.IMPERIAL


def test_explicit_preferences_win() -> None:
    user = make_user(country_code="US", preferred_currency="TWD", unit_system=UnitSystem.METRIC)
    assert effective_currency(user) == "TWD"
    assert effective_unit_system(user) is UnitSystem.METRIC


def test_has_location_flag() -> None:
    assert make_user().has_location is False
    assert make_user(locality="西螺鎮").has_location is True
    assert make_user(latitude=23.7, longitude=120.4).has_location is True


# ---------------------------------------------------------------------------
# 輸出 schema
# ---------------------------------------------------------------------------


def test_public_user_out_never_exposes_phone() -> None:
    """電話要不要露出是每一筆報價各自的決定，不在公開檔案裡。"""
    out = PublicUserOut.from_model(make_user(display_name="阿明"), "zh-Hant", active_quotes=3)
    assert "phone" not in out.model_dump()
    assert out.active_quote_count == 3


def test_country_out_flags_missing_subdivision_data() -> None:
    assert CountryOut.from_data(COUNTRIES["TW"], "zh-Hant").has_subdivision_data is True
    assert CountryOut.from_data(COUNTRIES["US"], "en").has_subdivision_data is True
    aruba = CountryOut.from_data(COUNTRIES[NO_SUBDIVISION_COUNTRY], "en")
    assert aruba.has_subdivision_data is False


def test_country_out_exposes_second_level_for_uganda() -> None:
    out = CountryOut.from_data(COUNTRIES["UG"], "zh-Hant")
    assert out.name == "烏干達"
    assert out.subdivision_label == "Region" and out.subdivision_count == 4
    assert out.has_second_level is True
    assert out.subdivision_label_level2 == "District"
    assert out.subdivision_count_level2 > 100


def test_subdivision_out_reports_hierarchy() -> None:
    region = SubdivisionOut.from_data(lookup_subdivision("UG-E"), "en")
    assert region.level == 1 and region.parent_code is None and region.has_children is True

    district = SubdivisionOut.from_data(lookup_subdivision("UG-203"), "en")
    assert district.level == 2 and district.parent_code == "UG-E"
    assert district.type == "District" and district.has_children is False


def test_subdivision_out_resolves_locale() -> None:
    sub = lookup_subdivision("JP-13")
    assert SubdivisionOut.from_data(sub, "ja").name == "東京都"
    assert SubdivisionOut.from_data(sub, "en").name == "Tokyo"
    assert SubdivisionOut.from_data(sub, "en").name_en == "Tokyo"
