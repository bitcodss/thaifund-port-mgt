"""
Transaction endpoints — Phase 1.
Every lot-mutating operation (BUY/SELL/SWITCH) is wrapped in a DB transaction
so the audit trail and lot balances are always consistent.
"""
import io
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.portfolio import Portfolio
from app.models.transaction import Transaction
from app.models.tax_lot import TaxLot
from app.models.user import User
from app.schemas.transaction import (
    TransactionCreate, TransactionOut, TaxLotOut, CsvImportResponse,
)
from app.api.deps import get_current_user
from app.services import transaction_service as svc
from app.services.csv_import import parse_csv
from app.services.lot_engine import InsufficientUnitsError
from app.services.portfolio_service import invalidate_portfolio
from sqlalchemy import select as sa_select, delete as sa_delete

router = APIRouter(prefix="/portfolios/{portfolio_id}/transactions", tags=["transactions"])
lots_router = APIRouter(prefix="/portfolios/{portfolio_id}/lots", tags=["lots"])


async def _get_portfolio(portfolio_id: uuid.UUID, db: AsyncSession, user: User) -> Portfolio:
    p = await db.get(Portfolio, portfolio_id)
    if not p:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    if p.user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    return p


@router.get("", response_model=list[TransactionOut])
async def list_transactions(
    portfolio_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _get_portfolio(portfolio_id, db, user)
    result = await db.execute(
        select(Transaction)
        .where(Transaction.portfolio_id == portfolio_id)
        .order_by(Transaction.date, Transaction.created_at)
    )
    return result.scalars().all()


@router.post("", response_model=TransactionOut, status_code=status.HTTP_201_CREATED)
async def add_transaction(
    portfolio_id: uuid.UUID,
    body: TransactionCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _get_portfolio(portfolio_id, db, user)

    tx = Transaction(
        id=uuid.uuid4(),
        portfolio_id=portfolio_id,
        **body.model_dump(),
    )
    db.add(tx)

    if body.type in ("BUY", "SELL") and not body.fund_code:
        raise HTTPException(status_code=400, detail="fund_code is required for BUY/SELL transactions")

    try:
        if body.type == "BUY":
            await svc.apply_buy(db, tx)
        elif body.type == "SELL":
            await svc.apply_sell(db, tx)
        elif body.type in {"SWITCH_OUT", "SWITCH_IN"}:
            # SWITCH requires both legs — handle via CSV or paired endpoint
            # Single-leg switch_in/out via the API is not valid alone;
            # the paired endpoint below is the correct entry point.
            raise HTTPException(
                status_code=400,
                detail="Use POST /switch to record a fund switch as a pair",
            )
        # DIVIDEND and INTEREST: no lot mutation, just persist the transaction

        await db.commit()
        await db.refresh(tx)
    except InsufficientUnitsError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

    invalidate_portfolio(portfolio_id)
    return tx


@router.post("/switch", response_model=list[TransactionOut], status_code=status.HTTP_201_CREATED)
async def add_switch(
    portfolio_id: uuid.UUID,
    switch_out: TransactionCreate,
    switch_in: TransactionCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Record a fund switch as a paired SWITCH_OUT + SWITCH_IN in one atomic operation."""
    await _get_portfolio(portfolio_id, db, user)

    if switch_out.type != "SWITCH_OUT" or switch_in.type != "SWITCH_IN":
        raise HTTPException(status_code=400, detail="Must supply one SWITCH_OUT and one SWITCH_IN")

    pair_id = str(uuid.uuid4())
    tx_out = Transaction(id=uuid.uuid4(), portfolio_id=portfolio_id, pair_id=pair_id, **switch_out.model_dump(exclude={"pair_id"}))
    tx_in = Transaction(id=uuid.uuid4(), portfolio_id=portfolio_id, pair_id=pair_id, **switch_in.model_dump(exclude={"pair_id"}))
    db.add(tx_out)
    db.add(tx_in)

    try:
        await svc.apply_switch(db, tx_out, tx_in)
        await db.commit()
        await db.refresh(tx_out)
        await db.refresh(tx_in)
    except InsufficientUnitsError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

    invalidate_portfolio(portfolio_id)
    return [tx_out, tx_in]


@router.post("/import-csv", response_model=CsvImportResponse)
async def import_csv(
    portfolio_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Parse a CSV file, validate all rows, then import valid ones."""
    await _get_portfolio(portfolio_id, db, user)

    content = await file.read()
    text = io.StringIO(content.decode("utf-8-sig"))  # handle BOM
    rows, parse_errors = parse_csv(text)

    if not rows and parse_errors:
        return CsvImportResponse(imported=0, errors=parse_errors)

    # Deduplicate against existing transactions
    existing_result = await db.execute(
        select(Transaction).where(Transaction.portfolio_id == portfolio_id)
    )
    existing_txns = existing_result.scalars().all()
    existing_keys = {
        (t.date, t.type, t.fund_code, t.units, t.amount)
        for t in existing_txns
    }

    imported = 0
    import_errors = list(parse_errors)

    # Separate SWITCH pairs for atomic processing
    switch_pairs: dict[str, list] = {}
    standalone: list = []
    for row in rows:
        if row.type in {"SWITCH_OUT", "SWITCH_IN"} and row.pair_id:
            switch_pairs.setdefault(row.pair_id, []).append(row)
        else:
            standalone.append(row)

    for row in standalone:
        key = (row.date, row.type, row.fund_code, row.units, row.amount)
        if key in existing_keys:
            import_errors.append(f"Duplicate skipped: {row.date} {row.type} {row.fund_code}")
            continue
        tx = Transaction(
            id=uuid.uuid4(),
            portfolio_id=portfolio_id,
            date=row.date,
            type=row.type,
            fund_code=row.fund_code,
            units=row.units,
            nav=row.nav,
            amount=row.amount,
            fee=row.fee,
            tax_withheld=row.tax_withheld,
            target_fund_code=row.target_fund_code,
            pair_id=row.pair_id,
            tax_scheme=row.tax_scheme,
            note=row.note,
        )
        try:
            async with db.begin_nested():
                db.add(tx)
                if row.type == "BUY":
                    await svc.apply_buy(db, tx)
                elif row.type == "SELL":
                    await svc.apply_sell(db, tx)
                # DIVIDEND, INTEREST: no lot mutation
            existing_keys.add(key)
            imported += 1
        except InsufficientUnitsError as e:
            import_errors.append(f"Row {row.date} {row.type} {row.fund_code}: {e}")

    for pair_id, pair_rows in switch_pairs.items():
        out_rows = [r for r in pair_rows if r.type == "SWITCH_OUT"]
        in_rows = [r for r in pair_rows if r.type == "SWITCH_IN"]
        if len(out_rows) != 1 or len(in_rows) != 1:
            import_errors.append(f"Switch pair '{pair_id}' incomplete, skipped")
            continue
        out_row, in_row = out_rows[0], in_rows[0]
        tx_out = Transaction(
            id=uuid.uuid4(), portfolio_id=portfolio_id,
            date=out_row.date, type=out_row.type, fund_code=out_row.fund_code,
            units=out_row.units, nav=out_row.nav, amount=out_row.amount,
            fee=out_row.fee, tax_withheld=out_row.tax_withheld,
            target_fund_code=out_row.target_fund_code, pair_id=pair_id,
            tax_scheme=out_row.tax_scheme, note=out_row.note,
        )
        tx_in = Transaction(
            id=uuid.uuid4(), portfolio_id=portfolio_id,
            date=in_row.date, type=in_row.type, fund_code=in_row.fund_code,
            units=in_row.units, nav=in_row.nav, amount=in_row.amount,
            fee=in_row.fee, tax_withheld=in_row.tax_withheld,
            target_fund_code=in_row.target_fund_code, pair_id=pair_id,
            tax_scheme=in_row.tax_scheme, note=in_row.note,
        )
        try:
            async with db.begin_nested():
                db.add(tx_out)
                db.add(tx_in)
                await svc.apply_switch(db, tx_out, tx_in)
            imported += 2
        except InsufficientUnitsError as e:
            import_errors.append(f"Switch pair '{pair_id}': {e}")

    await db.commit()
    if imported > 0:
        invalidate_portfolio(portfolio_id)
    return CsvImportResponse(imported=imported, errors=import_errors)


@router.delete("/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_transaction(
    portfolio_id: uuid.UUID,
    transaction_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _get_portfolio(portfolio_id, db, user)
    tx = await db.get(Transaction, transaction_id)
    if not tx or tx.portfolio_id != portfolio_id:
        raise HTTPException(status_code=404, detail="Transaction not found")

    lot_mutating = tx.type in ("BUY", "SELL", "SWITCH_OUT", "SWITCH_IN")

    if tx.type in ("SWITCH_OUT", "SWITCH_IN") and tx.pair_id:
        # Delete both legs of the switch atomically
        result = await db.execute(
            sa_select(Transaction).where(
                Transaction.portfolio_id == portfolio_id,
                Transaction.pair_id == tx.pair_id,
            )
        )
        for leg in result.scalars().all():
            await db.delete(leg)
    else:
        await db.delete(tx)

    if lot_mutating:
        await db.flush()
        await svc.rebuild_lots(portfolio_id, db)

    await db.commit()
    invalidate_portfolio(portfolio_id)


# ── Tax Lot endpoints ─────────────────────────────────────────────────────────

@lots_router.get("", response_model=list[TaxLotOut])
async def list_lots(
    portfolio_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _get_portfolio(portfolio_id, db, user)
    result = await db.execute(
        select(TaxLot)
        .where(TaxLot.portfolio_id == portfolio_id, TaxLot.units_remaining > 0)
        .order_by(TaxLot.original_purchase_date)
    )
    return result.scalars().all()
