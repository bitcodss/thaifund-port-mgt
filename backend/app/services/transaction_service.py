"""
DB layer that wraps lot_engine pure functions.
Every lot mutation lives inside a single DB transaction with an audit row.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tax_lot import TaxLot, LotConsumption
from app.models.transaction import Transaction
from app.services.lot_engine import (
    LotSnapshot,
    fifo_consume,
    build_switch_in_lots,
    InsufficientUnitsError,
)


async def _open_lots(
    db: AsyncSession,
    portfolio_id: UUID,
    fund_code: str,
    tax_scheme: str,
) -> list[LotSnapshot]:
    # FOR UPDATE: lock rows for the duration of the enclosing transaction so two
    # concurrent SELL/SWITCH calls against the same lots can't both succeed and
    # over-consume. No-op on SQLite (tests); enforced on PostgreSQL (prod).
    result = await db.execute(
        select(TaxLot).where(
            TaxLot.portfolio_id == portfolio_id,
            TaxLot.fund_code == fund_code,
            TaxLot.tax_scheme == tax_scheme,
            TaxLot.units_remaining > Decimal("0"),
        ).with_for_update()
    )
    lots = result.scalars().all()
    return [
        LotSnapshot(
            id=lot.id,
            fund_code=lot.fund_code,
            original_purchase_date=lot.original_purchase_date,
            units_remaining=lot.units_remaining,
            cost_basis_remaining=lot.cost_basis_remaining,
            tax_scheme=lot.tax_scheme,
        )
        for lot in lots
    ]


async def apply_buy(db: AsyncSession, tx: Transaction) -> TaxLot:
    """BUY: create a new tax lot. No lot consumption needed."""
    lot = TaxLot(
        id=uuid.uuid4(),
        portfolio_id=tx.portfolio_id,
        fund_code=tx.fund_code,
        original_purchase_date=tx.date,
        units_remaining=tx.units,
        cost_basis_remaining=tx.amount + tx.fee,
        tax_scheme=tx.tax_scheme,
        source_lot_id=None,
    )
    db.add(lot)
    return lot


async def apply_sell(db: AsyncSession, tx: Transaction) -> None:
    """SELL: FIFO consume lots, write audit rows, update lot balances."""
    snapshots = await _open_lots(db, tx.portfolio_id, tx.fund_code, tx.tax_scheme)
    consumptions = fifo_consume(snapshots, tx.units)

    lot_map = {s.id: s for s in snapshots}
    for c in consumptions:
        lot_row = await db.get(TaxLot, c.lot_id)
        lot_row.units_remaining -= c.units_consumed
        lot_row.cost_basis_remaining -= c.cost_basis_consumed
        db.add(LotConsumption(
            id=uuid.uuid4(),
            transaction_id=tx.id,
            lot_id=c.lot_id,
            units_consumed=c.units_consumed,
            cost_basis_consumed=c.cost_basis_consumed,
        ))


async def rebuild_lots(portfolio_id: UUID, db: AsyncSession) -> None:
    """
    Wipe all lots/consumptions for a portfolio and replay every transaction
    in chronological order. Called after any lot-mutating transaction is deleted.
    """
    tx_ids_subq = select(Transaction.id).where(Transaction.portfolio_id == portfolio_id)
    await db.execute(delete(LotConsumption).where(LotConsumption.transaction_id.in_(tx_ids_subq)))
    await db.execute(delete(TaxLot).where(TaxLot.portfolio_id == portfolio_id))

    result = await db.execute(
        select(Transaction)
        .where(Transaction.portfolio_id == portfolio_id)
        .order_by(Transaction.date, Transaction.created_at)
    )
    txs = result.scalars().all()

    # Pre-index pairs by pair_id for O(N) lookup instead of O(N) scans per pair
    pairs_by_id: dict[str, list[Transaction]] = {}
    for tx in txs:
        if tx.pair_id:
            pairs_by_id.setdefault(tx.pair_id, []).append(tx)

    processed_pairs: set[str] = set()
    for tx in txs:
        if tx.type == "BUY":
            await apply_buy(db, tx)
        elif tx.type == "SELL":
            await apply_sell(db, tx)
        elif tx.type in ("SWITCH_OUT", "SWITCH_IN") and tx.pair_id:
            if tx.pair_id in processed_pairs:
                continue
            processed_pairs.add(tx.pair_id)
            pair = pairs_by_id.get(tx.pair_id, [])
            out_tx = next((t for t in pair if t.type == "SWITCH_OUT"), None)
            in_tx = next((t for t in pair if t.type == "SWITCH_IN"), None)
            if not (out_tx and in_tx):
                raise ValueError(
                    f"SWITCH pair_id={tx.pair_id} is incomplete during rebuild "
                    f"(found {[t.type for t in pair]}); refusing to silently lose units"
                )
            await apply_switch(db, out_tx, in_tx)
        # DIVIDEND and INTEREST: no lot mutation


async def apply_switch(
    db: AsyncSession,
    switch_out_tx: Transaction,
    switch_in_tx: Transaction,
) -> None:
    """
    SWITCH: FIFO close source lots, create new lots in target fund.
    Both transactions are already persisted before this is called.
    """
    snapshots = await _open_lots(
        db,
        switch_out_tx.portfolio_id,
        switch_out_tx.fund_code,
        switch_out_tx.tax_scheme,
    )
    consumptions = fifo_consume(snapshots, switch_out_tx.units)
    source_map = {s.id: s for s in snapshots}

    # Close (or partially close) source lots
    for c in consumptions:
        lot_row = await db.get(TaxLot, c.lot_id)
        lot_row.units_remaining -= c.units_consumed
        lot_row.cost_basis_remaining -= c.cost_basis_consumed
        db.add(LotConsumption(
            id=uuid.uuid4(),
            transaction_id=switch_out_tx.id,
            lot_id=c.lot_id,
            units_consumed=c.units_consumed,
            cost_basis_consumed=c.cost_basis_consumed,
        ))

    # Create new lots in target fund using actual units from the fund house statement
    new_lot_defs = build_switch_in_lots(
        consumptions,
        source_map,
        switch_in_tx.fund_code,
        switch_in_tx.nav,
        switch_in_tx.units,
    )
    for nl in new_lot_defs:
        new_lot = TaxLot(
            id=uuid.uuid4(),
            portfolio_id=switch_in_tx.portfolio_id,
            fund_code=nl.fund_code,
            original_purchase_date=nl.original_purchase_date,
            units_remaining=nl.units_remaining,
            cost_basis_remaining=nl.cost_basis_remaining,
            tax_scheme=nl.tax_scheme,
            source_lot_id=nl.source_lot_id,
        )
        db.add(new_lot)
