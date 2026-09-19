"""add consumer price intents

Revision ID: 493db1b00d94
Revises: 050a08e79979
Create Date: 2026-09-19 20:02:22.187729+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '493db1b00d94'
down_revision: str | None = '050a08e79979'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 只建立意向相關的三張表。autogenerate 另外想改 users 兩個欄位的
    # server_default，那是它比對不出 Python 端 default 的誤判，已移除。
    op.create_table('price_intents',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('product_id', sa.UUID(), nullable=False),
    sa.Column('price', sa.Numeric(precision=14, scale=4), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('unit', sa.String(length=16), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=16, scale=3), nullable=True),
    sa.Column('country_code', sa.String(length=2), nullable=False),
    sa.Column('region', sa.String(length=80), nullable=True),
    sa.Column('latitude', sa.Float(), nullable=True),
    sa.Column('longitude', sa.Float(), nullable=True),
    sa.Column('distance_km', sa.Float(), nullable=True),
    sa.Column('status', sa.Enum('active', 'superseded', 'withdrawn', name='intent_status'), nullable=False),
    sa.Column('excluded_reason', sa.Enum('below_floor', 'outlier', 'shadowed', 'non_local', 'untrusted_ip', 'zero_weight', name='intent_exclusion'), nullable=True),
    sa.Column('weight_snapshot', sa.Float(), nullable=False),
    sa.Column('floor_price', sa.Numeric(precision=14, scale=4), nullable=True),
    sa.Column('ip_hosting', sa.Boolean(), nullable=False),
    sa.Column('ip_proxy', sa.Boolean(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_price_intents_product_id_products'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_price_intents_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_price_intents'))
    )
    op.create_index('ix_price_intents_country_code_status', 'price_intents', ['country_code', 'status'], unique=False)
    op.create_index('ix_price_intents_product_id_region_status', 'price_intents', ['product_id', 'region', 'status'], unique=False)
    op.create_index(op.f('ix_price_intents_region'), 'price_intents', ['region'], unique=False)
    op.create_index('ix_price_intents_user_id_product_id_created_at', 'price_intents', ['user_id', 'product_id', 'created_at'], unique=False)
    op.create_table('user_reputation',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('weight', sa.Float(), nullable=False),
    sa.Column('samples', sa.Integer(), nullable=False),
    sa.Column('hits', sa.Integer(), nullable=False),
    sa.Column('misses', sa.Integer(), nullable=False),
    sa.Column('consecutive_misses', sa.Integer(), nullable=False),
    sa.Column('is_shadow_banned', sa.Boolean(), nullable=False),
    sa.Column('shadow_banned_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('has_verified_purchase', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_reputation_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', name=op.f('pk_user_reputation'))
    )
    op.create_table('intent_notifications',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('intent_id', sa.UUID(), nullable=True),
    sa.Column('product_id', sa.UUID(), nullable=False),
    sa.Column('quote_id', sa.UUID(), nullable=True),
    sa.Column('offer_price', sa.Numeric(precision=14, scale=4), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('unit', sa.String(length=16), nullable=False),
    sa.Column('intent_price', sa.Numeric(precision=14, scale=4), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('opened_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('clicked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['intent_id'], ['price_intents.id'], name=op.f('fk_intent_notifications_intent_id_price_intents'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_intent_notifications_product_id_products'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['quote_id'], ['quotes.id'], name=op.f('fk_intent_notifications_quote_id_quotes'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_intent_notifications_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_intent_notifications'))
    )
    op.create_index('ix_intent_notifications_quote_id', 'intent_notifications', ['quote_id'], unique=False)
    op.create_index('ix_intent_notifications_user_id_sent_at', 'intent_notifications', ['user_id', 'sent_at'], unique=False)
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index('ix_intent_notifications_user_id_sent_at', table_name='intent_notifications')
    op.drop_index('ix_intent_notifications_quote_id', table_name='intent_notifications')
    op.drop_table('intent_notifications')
    op.drop_table('user_reputation')
    op.drop_index('ix_price_intents_user_id_product_id_created_at', table_name='price_intents')
    op.drop_index(op.f('ix_price_intents_region'), table_name='price_intents')
    op.drop_index('ix_price_intents_product_id_region_status', table_name='price_intents')
    op.drop_index('ix_price_intents_country_code_status', table_name='price_intents')
    op.drop_table('price_intents')
    # autogenerate 不會清 enum 型別，不補這兩行的話 downgrade 後再 upgrade
    # 會撞到「type already exists」
    sa.Enum(name='intent_status').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='intent_exclusion').drop(op.get_bind(), checkfirst=True)
    # ### end Alembic commands ###
