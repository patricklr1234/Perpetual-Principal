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
        "STALE CLOSED RANGE LEDGER LOTS ZEROED | ids=%s | reason=%s | history_preserved=True",
        ids,
        reason,
    )
    return len(ids)


def repair_stale_closed_range_ownership(reconciler):
    if not bot.LIVE_TRADING:
        return []

    ledger = reconciler.expected_by_symbol_side()
    snap1 = reconciler.snapshot()
    physical1 = snap1.positions
    state1 = reconciler.expected_from_state_by_symbol_side()
    repaired = []

    for key in sorted(set(ledger) | set(physical1) | set(state1)):
        symbol, side = key
        if symbol not in reconciler.rules.rules or side not in ("LONG", "SHORT"):
            continue
        step = reconciler.rules.rules[symbol].step_size
        lq = ledger.get(key, bot.D(0))
        pq = physical1.get(key, bot.D(0))
        sq = state1.get(key, bot.D(0))

        if abs(pq - sq) >= step or lq - pq < step:
            continue

        stale = _candidate_stale_lots(reconciler, symbol, side, pq, sq, lq)
        if not stale:
            continue

        stale_qty = sum((bot.dec(lot["qty"]) for lot in stale), bot.D(0))
        owners = {str(lot["strategy_id"]) for lot in stale}
        live_orders1 = _live_orders_for_stale_owners(reconciler, snap1, symbol, side, owners)
        if live_orders1:
            bot.logger.error(
                "STALE CLOSED RANGE REPAIR ABORT | %s %s | stale owners still have live orders=%s",
                symbol, side, live_orders1,
            )
            continue

        bot.logger.warning(
            "STALE CLOSED RANGE REPAIR CANDIDATE | %s %s | ledger=%s state=%s physical=%s "
            "stale_qty=%s owners=%s ids=%s",
            symbol, side, lq, sq, pq, stale_qty, sorted(owners), [lot["id"] for lot in stale],
        )

        snap2 = reconciler.snapshot()
        p2 = snap2.positions.get(key, bot.D(0))
        s2 = reconciler.expected_from_state_by_symbol_side().get(key, bot.D(0))
        l2 = reconciler.expected_by_symbol_side().get(key, bot.D(0))
        if abs(p2 - pq) >= step or abs(s2 - sq) >= step or abs(l2 - lq) >= step:
            raise RuntimeError(
                f"candidate changed during verification {symbol} {side}: "
                f"p {pq}->{p2} s {sq}->{s2} l {lq}->{l2}"
            )
        if _live_orders_for_stale_owners(reconciler, snap2, symbol, side, owners):
            raise RuntimeError(f"stale owner order appeared during verification {symbol} {side}")

        stale2 = _candidate_stale_lots(reconciler, symbol, side, p2, s2, l2)
        ids1 = sorted(str(x["id"]) for x in stale)
        ids2 = sorted(str(x["id"]) for x in stale2)
        if ids1 != ids2:
            raise RuntimeError(f"stale id set changed during verification {ids1}->{ids2}")

        count = _zero_exact_leg_ids(
            reconciler,
            stale2,
            reason=f"state_equals_physical_exact_ledger_excess:{symbol}:{side}:qty={stale_qty}",
        )
        if count != len(stale2):
            raise RuntimeError(f"unexpected stale row count {count}/{len(stale2)}")

        snap3 = reconciler.snapshot()
        p3 = snap3.positions.get(key, bot.D(0))
        s3 = reconciler.expected_from_state_by_symbol_side().get(key, bot.D(0))
        l3 = reconciler.expected_by_symbol_side().get(key, bot.D(0))
        if abs(p3 - pq) >= step or abs(s3 - sq) >= step:
            raise RuntimeError(
                f"physical/state changed after ledger-only repair {symbol} {side}: "
                f"physical {pq}->{p3} state {sq}->{s3}"
            )
        if abs(l3 - p3) >= step or abs(l3 - s3) >= step:
            raise RuntimeError(
                f"ledger repair did not converge {symbol} {side}: physical={p3} state={s3} ledger={l3}"
            )

        repaired.append((symbol, side, stale_qty, ids1))
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
