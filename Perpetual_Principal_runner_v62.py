#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Perpetual Principal production entrypoint v62.

Adds a fail-closed stale closed-RANGE ledger repair on top of runner.py.

Problem addressed:
- durable FillLedger may keep old open_qty for RANGE grid lots whose strategy state
  is already flat/IDLE or PROTECT;
- exchange physical position can already equal the live state, while ledger is larger;
- existing reconciliation repairs only exchange-flat sides or state-orphan cases where
  ledger == physical, so this third case remained blocked until manual intervention.

Safety invariant for automatic repair:
- physical quantity == state quantity for the exact symbol/side;
- ledger quantity > physical quantity;
- ledger excess equals exactly the sum of durable lots absent from live state;
- every stale lot belongs to a current RANGE G0..G3 strategy;
- every stale owning grid has no basket and is IDLE or PROTECT;
- no active exchange order on that Hedge-Mode side belongs to a stale owner;
- physical/state are proven unchanged twice before mutation;
- only the proven stale leg_ids have open_qty set to zero;
- physical/state are proven unchanged after mutation and ledger must equal them.

No market order is sent. No state, bankroll, realized PnL, recovery deficit,
strategy/risk parameter, BOT_DIR or Volume is changed.
"""

import main as bot
import runner

_original_reconcile = bot.Reconciler.reconcile


def _represented_leg_ids(reconciler):
    return reconciler._represented_state_leg_ids()


def _grid_state(reconciler, strategy_id):
    return reconciler._range_state_for_strategy(strategy_id)


def _live_orders_for_stale_owners(reconciler, snap, symbol, side, owners):
    found = []
    for order in snap.open_orders or []:
        if str(order.get("symbol") or "").upper() != symbol:
            continue
        if str(order.get("positionSide") or "").upper() != side:
            continue
        if str(order.get("status") or "NEW").upper() not in ("NEW", "PARTIALLY_FILLED"):
            continue
        cid = str(order.get("clientOrderId") or order.get("origClientOrderId") or "")
        if not cid:
            raise RuntimeError(f"active order without clientOrderId on {symbol} {side}")
        owner = reconciler.ledger.order_owner(cid)
        if owner is None:
            raise RuntimeError(f"active order without durable owner on {symbol} {side}: {cid}")
        if any(owner == sid or owner.startswith(sid + ":") for sid in owners):
            found.append((cid, owner))
    return found


def _candidate_stale_lots(reconciler, symbol, side, physical_qty, state_qty, ledger_qty):
    step = reconciler.rules.rules[symbol].step_size
    if abs(physical_qty - state_qty) >= step:
        return []
    excess = ledger_qty - physical_qty
    if excess < step:
        return []

    represented = _represented_leg_ids(reconciler)
    lots = reconciler.ledger.open_lots_for_symbol_side(symbol, side)
    stale = [lot for lot in lots if str(lot.get("id") or "") not in represented]
    stale_qty = sum((bot.dec(lot.get("qty")) for lot in stale), bot.D(0))
    if abs(stale_qty - excess) >= step or not stale:
        return []

    owners = {str(lot.get("strategy_id") or "") for lot in stale}
    if not owners or not all(reconciler._is_current_range_grid_strategy(sid, symbol) for sid in owners):
        return []

    for sid in owners:
        st = _grid_state(reconciler, sid)
        if not isinstance(st, dict):
            return []
        if st.get("basket"):
            return []
        if str(st.get("status") or "IDLE").upper() not in ("IDLE", "PROTECT"):
            return []

    return stale


def _zero_exact_leg_ids(reconciler, stale_lots, reason):
    ids = [str(lot["id"]) for lot in stale_lots]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    expected = {str(lot["id"]): bot.dec(lot["qty"]) for lot in stale_lots}

    with reconciler.ledger.lock:
        reconciler.ledger.db.execute("BEGIN IMMEDIATE")
        rows = reconciler.ledger.db.execute(
            f"SELECT leg_id, open_qty FROM lots WHERE leg_id IN ({placeholders})",
            ids,
        ).fetchall()
        current = {str(leg_id): bot.dec(open_qty) for leg_id, open_qty in rows}
        if current != expected:
            reconciler.ledger.db.rollback()
            raise RuntimeError(f"stale leg set changed before commit: expected={expected} current={current}")
        t = bot.now_ms()
        reconciler.ledger.db.execute(
            f"UPDATE lots SET open_qty='0', closed_ms=COALESCE(closed_ms, ?) "
            f"WHERE leg_id IN ({placeholders}) AND CAST(open_qty AS REAL)>0",
            [t] + ids,
        )
        reconciler.ledger.db.commit()

    bot.logger.warning(
        "LEDGER EXACT STALE-OWNER REPAIR | leg_ids=%s | reason=%s",
        ids, reason,
    )
    return len(ids)


def repair_stale_closed_range_ownership(reconciler):
    if not bot.LIVE_TRADING:
        return []

    repaired = []
    snap1 = reconciler.snapshot()
    physical1 = dict(snap1.positions)
    state1 = reconciler.expected_from_state_by_symbol_side()
    ledger1 = reconciler.expected_by_symbol_side()

    keys = set(ledger1) | set(physical1) | set(state1)
    for symbol, side in sorted(keys):
        if symbol not in bot.SYMBOLS or side not in ("LONG", "SHORT"):
            continue
        step = reconciler.rules.rules[symbol].step_size
        p1 = physical1.get((symbol, side), bot.D(0))
        s1 = state1.get((symbol, side), bot.D(0))
        l1 = ledger1.get((symbol, side), bot.D(0))

        stale = _candidate_stale_lots(reconciler, symbol, side, p1, s1, l1)
        if not stale:
            continue

        owners = {str(lot["strategy_id"]) for lot in stale}
        stale_qty = sum((bot.dec(lot["qty"]) for lot in stale), bot.D(0))
        if _live_orders_for_stale_owners(reconciler, snap1, symbol, side, owners):
            bot.logger.error(
                "STALE CLOSED RANGE REPAIR ABORT | %s %s | stale owner still has live orders | owners=%s",
                symbol, side, sorted(owners),
            )
            continue

        # Second independent proof immediately before durable mutation.
        snap2 = reconciler.snapshot()
        p2 = snap2.positions.get((symbol, side), bot.D(0))
        state2 = reconciler.expected_from_state_by_symbol_side()
        s2 = state2.get((symbol, side), bot.D(0))
        ledger2 = reconciler.expected_by_symbol_side()
        l2 = ledger2.get((symbol, side), bot.D(0))
        stale2 = _candidate_stale_lots(reconciler, symbol, side, p2, s2, l2)
        ids1 = {str(x["id"]) for x in stale}
        ids2 = {str(x["id"]) for x in stale2}

        if abs(p2 - p1) >= step or abs(s2 - s1) >= step or ids2 != ids1:
            bot.logger.error(
                "STALE CLOSED RANGE REPAIR ABORT | %s %s | verification changed | "
                "physical=%s->%s state=%s->%s ids=%s->%s",
                symbol, side, p1, p2, s1, s2, sorted(ids1), sorted(ids2),
            )
            continue
        if _live_orders_for_stale_owners(reconciler, snap2, symbol, side, owners):
            bot.logger.error(
                "STALE CLOSED RANGE REPAIR ABORT | %s %s | stale owner order appeared during verification",
                symbol, side,
            )
            continue

        reason = (
            f"state_equals_physical={p2}; ledger={l2}; stale_qty={stale_qty}; "
            f"owners={sorted(owners)}; no_market_order"
        )
        _zero_exact_leg_ids(reconciler, stale2, reason)

        # Post-mutation proof. Exchange/state must not move, and ledger must converge exactly.
        snap3 = reconciler.snapshot()
        p3 = snap3.positions.get((symbol, side), bot.D(0))
        s3 = reconciler.expected_from_state_by_symbol_side().get((symbol, side), bot.D(0))
        l3 = reconciler.expected_by_symbol_side().get((symbol, side), bot.D(0))
        if abs(p3 - p2) >= step or abs(s3 - s2) >= step or abs(l3 - p3) >= step:
            raise RuntimeError(
                f"post stale-owner repair mismatch {symbol} {side}: "
                f"physical={p3} state={s3} ledger={l3}"
            )

        repaired.append((symbol, side, stale_qty, sorted(owners)))
        bot.logger.warning(
            "STALE CLOSED RANGE REPAIR VERIFIED | %s %s | stale_qty=%s owners=%s | "
            "physical=state=ledger=%s | positions/orders untouched",
            symbol, side, stale_qty, sorted(owners), p3,
        )

    return repaired


def _reconcile_with_stale_closed_range_repair(self):
    try:
        repaired = repair_stale_closed_range_ownership(self)
        if repaired:
            bot.logger.warning("STALE CLOSED RANGE REPAIR COMPLETE | repaired=%s", repaired)
    except Exception as exc:
        reason = f"STALE_CLOSED_RANGE_REPAIR_FAILED:{type(exc).__name__}:{exc}"
        self.store.set_trade_gate(False, reason)
        bot.logger.exception("STALE CLOSED RANGE REPAIR FAIL-CLOSED | %s", reason)
        return False
    return _original_reconcile(self)


bot.Reconciler.reconcile = _reconcile_with_stale_closed_range_repair
bot.VERSION = f"{bot.VERSION}-stale-closed-range-ledger-repair-v62"


def main():
    bot.logger.warning(
        "STALE CLOSED RANGE LEDGER REPAIR ACTIVE | version=v62 | "
        "policy=state==physical; exact-unrepresented-ledger-excess-only; no-market-order"
    )
    runner.main()


if __name__ == "__main__":
    main()
