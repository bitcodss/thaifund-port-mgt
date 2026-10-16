"""chunk 4 peer metrics: factsheet hedge policy, hedge source, fx_rates, peer_snapshots

Revision ID: 012
Revises: 011
Create Date: 2026-10-17 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("fund_factsheets", sa.Column("fx_hedge_policy", sa.String(12), nullable=True))
    op.add_column("peer_profiles", sa.Column("hedge_source", sa.String(12), nullable=True))
    op.create_table(
        "fx_rates",
        sa.Column("pair", sa.String(10), primary_key=True),
        sa.Column("trade_date", sa.Date, primary_key=True),
        sa.Column("rate", sa.Numeric(20, 8), nullable=False),
    )
    n = lambda: sa.Numeric(20, 8)
    op.create_table(
        "peer_snapshots",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("sec_proj_id", sa.String(50), nullable=False, index=True),
        sa.Column("month", sa.Date, nullable=False, index=True),
        sa.Column("nav_code", sa.String(50)),
        sa.Column("aimc_category", sa.String(150)),
        sa.Column("peer_theme", sa.String(150)),
        sa.Column("basis", sa.String(10)),
        sa.Column("flag_eligible", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("hedge_group", sa.String(12)),
        sa.Column("hedge_basis", sa.String(12)),
        sa.Column("peer_masters", sa.Integer),
        sa.Column("fx_adjusted_masters", sa.Integer),
        *[sa.Column(f"{k}_{h}", n()) for k in ("ret", "median") for h in ("1m", "3m", "6m", "1y")],
        *[sa.Column(f"pct_{h}", sa.Numeric(6, 2)) for h in ("1m", "3m", "6m", "1y")],
        sa.Column("z_1m", sa.Numeric(10, 4)),
        sa.Column("z_3m", sa.Numeric(10, 4)),
        sa.Column("fx_effect_1m", n()),
        sa.Column("fx_effect_3m", n()),
        sa.Column("hedge_ratio", sa.Numeric(6, 4)),
        sa.Column("hedge_ratio_estimated", sa.Boolean),
        sa.Column("details", sa.JSON),
        sa.Column("backfilled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("sec_proj_id", "month", name="uq_peer_snapshot_month"),
    )


def downgrade() -> None:
    op.drop_table("peer_snapshots")
    op.drop_table("fx_rates")
    op.drop_column("peer_profiles", "hedge_source")
    op.drop_column("fund_factsheets", "fx_hedge_policy")
