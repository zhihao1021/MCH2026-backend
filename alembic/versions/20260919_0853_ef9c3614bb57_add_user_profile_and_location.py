"""add user profile and location

新增個人檔案（商號、簡介、頭像、網站）與結構化位置
（ISO 3166-2 行政區、城鎮、地址、郵遞區號、座標、公開程度）。

舊的 `users.region` 是單一自由文字欄位，隱含了「一層行政區就夠」的假設；
這裡把它拆成 `subdivision_code`（有收錄清單的國家）與 `locality`（其餘國家），
既有資料一律搬進 `locality`——原本存的就是自由文字，無法安全地反推成代碼。

Revision ID: ef9c3614bb57
Revises: b6de97a2d3b3
Create Date: 2026-09-19 08:53:39.457033+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ef9c3614bb57"
down_revision: str | None = "b6de97a2d3b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# 各國預設時區，與 app/data/countries.py 一致。
# 這裡寫死是刻意的：migration 必須反映「執行當下」的資料，
# 不能跟著日後會變動的應用程式常數跑。
_TIMEZONE_BY_COUNTRY = {
    "TW": "Asia/Taipei",
    "JP": "Asia/Tokyo",
    "KR": "Asia/Seoul",
    "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "SG": "Asia/Singapore",
    "MY": "Asia/Kuala_Lumpur",
    "TH": "Asia/Bangkok",
    "VN": "Asia/Ho_Chi_Minh",
    "PH": "Asia/Manila",
    "ID": "Asia/Jakarta",
    "US": "America/New_York",
}


def upgrade() -> None:
    # 用原生 SQL 建 enum 型別，而不是讓 add_column 隱式建立：
    # 隱式建立的型別在 downgrade 時不會被刪掉，而且 `alembic --sql`
    # 的離線模式跑不了 checkfirst 需要的查詢。
    op.execute("CREATE TYPE unit_system AS ENUM ('metric', 'imperial')")
    op.execute(
        "CREATE TYPE location_visibility AS ENUM "
        "('exact', 'approximate', 'region', 'private')"
    )

    # ---- 個人檔案 ----
    op.add_column("users", sa.Column("business_name", sa.String(length=120), nullable=True))
    op.add_column("users", sa.Column("bio", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("avatar_url", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("website_url", sa.Text(), nullable=True))

    # ---- 在地化偏好 ----
    # NULL = 跟著國家預設走，所以這兩欄都可為空
    op.add_column("users", sa.Column("preferred_currency", sa.String(length=3), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "unit_system",
            postgresql.ENUM(
                "metric", "imperial", name="unit_system", create_type=False
            ),
            nullable=True,
        ),
    )
    op.add_column("users", sa.Column("timezone", sa.String(length=64), nullable=True))

    # ---- 位置 ----
    op.add_column("users", sa.Column("subdivision_code", sa.String(length=8), nullable=True))
    op.add_column("users", sa.Column("locality", sa.String(length=120), nullable=True))
    op.add_column("users", sa.Column("address_line", sa.String(length=200), nullable=True))
    op.add_column("users", sa.Column("postal_code", sa.String(length=16), nullable=True))
    op.add_column("users", sa.Column("latitude", sa.Float(), nullable=True))
    op.add_column("users", sa.Column("longitude", sa.Float(), nullable=True))
    # server_default 不可省：既有列必須有值才能設成 NOT NULL
    op.add_column(
        "users",
        sa.Column(
            "location_visibility",
            postgresql.ENUM(
                "exact", "approximate", "region", "private",
                name="location_visibility", create_type=False,
            ),
            nullable=False,
            server_default="region",
        ),
    )
    op.add_column(
        "users", sa.Column("location_updated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "users",
        sa.Column(
            "contact_phone_public", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )

    # ---- 搬移既有資料 ----
    # 舊的 region 是自由文字（「雲林縣」），沒辦法安全地反推成 ISO 3166-2 代碼，
    # 所以一律進 locality；使用者之後在 App 裡重新選一次行政區即可。
    op.execute(
        "UPDATE users SET locality = region WHERE region IS NOT NULL AND region <> ''"
    )
    op.execute(
        "UPDATE users SET location_updated_at = updated_at WHERE locality IS NOT NULL"
    )

    # 依國家補上時區，讓「今日行情」的日界線一開始就是對的
    case_sql = " ".join(
        f"WHEN '{code}' THEN '{tz}'" for code, tz in _TIMEZONE_BY_COUNTRY.items()
    )
    op.execute(
        f"UPDATE users SET timezone = CASE country_code {case_sql} ELSE NULL END"
    )

    op.create_index(
        "ix_users_country_code_subdivision_code",
        "users",
        ["country_code", "subdivision_code"],
        unique=False,
    )
    op.drop_column("users", "region")


def downgrade() -> None:
    op.add_column(
        "users",
        sa.Column("region", sa.VARCHAR(length=80), autoincrement=False, nullable=True),
    )
    # 搬回去。locality 上限 120 但 region 只有 80，超長的截斷而不是讓整個 migration 失敗
    op.execute(
        "UPDATE users SET region = LEFT(locality, 80) WHERE locality IS NOT NULL"
    )

    op.drop_index("ix_users_country_code_subdivision_code", table_name="users")
    for column in (
        "contact_phone_public",
        "location_updated_at",
        "location_visibility",
        "longitude",
        "latitude",
        "postal_code",
        "address_line",
        "locality",
        "subdivision_code",
        "timezone",
        "unit_system",
        "preferred_currency",
        "website_url",
        "avatar_url",
        "bio",
        "business_name",
    ):
        op.drop_column("users", column)

    op.execute("DROP TYPE location_visibility")
    op.execute("DROP TYPE unit_system")
