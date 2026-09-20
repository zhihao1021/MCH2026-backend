"""新增消費者回報的零售價（retail_price_reports）

Revision ID: 2d52d9ed9c86
Revises: ecfe49743e19
Create Date: 2026-09-20 02:36:53.882520+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = '2d52d9ed9c86'
down_revision: str | None = 'ecfe49743e19'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('retail_price_reports',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('product_id', sa.UUID(), nullable=False),
    sa.Column('observed_price', sa.Numeric(precision=14, scale=4), nullable=False),
    sa.Column('pack_size', sa.Numeric(precision=12, scale=4), nullable=True),
    sa.Column('unit_price', sa.Numeric(precision=14, scale=4), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('unit', sa.String(length=16), nullable=False),
    sa.Column('is_promotion', sa.Boolean(), nullable=False),
    sa.Column('store_type', sa.Enum('supermarket', 'hypermarket', 'convenience', 'wet_market', 'grocery', 'online', 'cooperative', 'other', name='store_type'), nullable=False),
    sa.Column('store_name', sa.String(length=120), nullable=False),
    sa.Column('store_branch', sa.String(length=120), nullable=True),
    sa.Column('store_key', sa.String(length=160), nullable=False),
    sa.Column('country_code', sa.String(length=2), nullable=False),
    sa.Column('subdivision_code', sa.String(length=8), nullable=True),
    sa.Column('region', sa.String(length=80), nullable=True),
    sa.Column('location_text', sa.String(length=200), nullable=True),
    sa.Column('latitude', sa.Float(), nullable=True),
    sa.Column('longitude', sa.Float(), nullable=True),
    sa.Column('observed_on', sa.Date(), nullable=False),
    sa.Column('photo_url', sa.Text(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('status', sa.Enum('active', 'withdrawn', 'hidden', name='retail_report_status'), nullable=False),
    sa.Column('excluded_reason', sa.Enum('outlier', 'shadowed', 'untrusted_ip', 'zero_weight', name='retail_exclusion'), nullable=True),
    sa.Column('weight_snapshot', sa.Float(), nullable=False),
    sa.Column('ip_hosting', sa.Boolean(), nullable=False),
    sa.Column('ip_proxy', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_retail_price_reports_product_id_products'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_retail_price_reports_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_retail_price_reports'))
    )
    op.create_index(op.f('ix_retail_price_reports_country_code'), 'retail_price_reports', ['country_code'], unique=False)
    op.create_index('ix_retail_price_reports_country_code_region', 'retail_price_reports', ['country_code', 'region'], unique=False)
    op.create_index(op.f('ix_retail_price_reports_observed_on'), 'retail_price_reports', ['observed_on'], unique=False)
    op.create_index('ix_retail_price_reports_product_id_status_observed_on', 'retail_price_reports', ['product_id', 'status', 'observed_on'], unique=False)
    op.create_index(op.f('ix_retail_price_reports_status'), 'retail_price_reports', ['status'], unique=False)
    op.create_index(op.f('ix_retail_price_reports_store_key'), 'retail_price_reports', ['store_key'], unique=False)
    op.create_index(op.f('ix_retail_price_reports_subdivision_code'), 'retail_price_reports', ['subdivision_code'], unique=False)
    op.create_index('ix_retail_price_reports_user_id_created_at', 'retail_price_reports', ['user_id', 'created_at'], unique=False)
    op.create_index('ix_retail_price_reports_user_id_product_id_store_key', 'retail_price_reports', ['user_id', 'product_id', 'store_key'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_retail_price_reports_user_id_product_id_store_key', table_name='retail_price_reports')
    op.drop_index('ix_retail_price_reports_user_id_created_at', table_name='retail_price_reports')
    op.drop_index(op.f('ix_retail_price_reports_subdivision_code'), table_name='retail_price_reports')
    op.drop_index(op.f('ix_retail_price_reports_store_key'), table_name='retail_price_reports')
    op.drop_index(op.f('ix_retail_price_reports_status'), table_name='retail_price_reports')
    op.drop_index('ix_retail_price_reports_product_id_status_observed_on', table_name='retail_price_reports')
    op.drop_index(op.f('ix_retail_price_reports_observed_on'), table_name='retail_price_reports')
    op.drop_index('ix_retail_price_reports_country_code_region', table_name='retail_price_reports')
    op.drop_index(op.f('ix_retail_price_reports_country_code'), table_name='retail_price_reports')
    op.drop_table('retail_price_reports')
    # drop_table 不會連帶刪掉 enum 型別，沒清乾淨的話再 upgrade 一次會
    # 撞上 "type already exists"
    bind = op.get_bind()
    for name in ('retail_exclusion', 'retail_report_status', 'store_type'):
        sa.Enum(name=name).drop(bind, checkfirst=True)
