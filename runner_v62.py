#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Perpetual Principal production entrypoint v63.

Fixes stale closed RANGE ownership precedence during partial/external reductions.

Core invariant:
- closed/IDLE RANGE owners with no basket must never reserve physical capacity from a live RANGE owner;
- if exactly one live RANGE owner remains on a symbol/side and stale closed RANGE ledger lots plus
  an externally reduced physical position created ledger/state drift, repair ownership only when
  the current physical quantity can be assigned unambiguously to that live owner;
- repair is ledger/state/protection only: no market order, no position mutation, no bankroll/PnL/RD/risk changes.
"""

import main as bot
import runner

# Preserve the v62 reconciler behavior by carrying its stale-closed repair forward.
_original_reconcile = bot.Reconciler.reconcile
_original_other_reserved = bot.RangeEngine._other_strategy_reserved_qty


def _state_for_range(store, strategy_id):
    parts = str(strategy_id).split(":")
    if len(parts) != 3 or parts[0] != "RANGE":
        return None
    symbol, gid = parts[1].upper(), parts[2]
    return store.state.get("range_grids", {}).get(f"{symbol}:{gid}")


def _live_range_state_qty(store, symbol, side, exclude_strategy=None):
    total = bot.D(0)
    with store.lock:
        for key, st in store.state.get("range_grids", {}).items():
            if not isinstance(st, dict):
                continue
            sid = str(st.get("strategy") or f"RANGE:{key}")
            if exclude_strategy and sid == exclude_strategy:
                continue
            if str(st.get("symbol") or "").upper() != symbol:
                continue
            basket = st.get("basket") or {}
            for leg in basket.get("legs", []) or []:
                if str(leg.get("side") or "").upper() == side:
                    total += bot.dec(leg.get("qty"))
    return total


def _other_strategy_reserved_qty_live_state(self, position_side):
    """Reserve physical capacity from live state, never from stale ledger ownership.

    The old implementation used total ledger minus own ledger. A closed RANGE owner whose durable
    lot had not yet been zeroed therefore stole capacity from the active grid and caused
    RANGE GHOST LEG REDUZIDA on the valid basket. State is the authoritative reservation source
    for *other live RANGE grids*; MACD reservation remains represented in state as well.
    """
    side = str(position_side).upper()
    symbol = str(self.symbol).upper()
    reserved = _live_range_state_qty(self.store, symbol, side, exclude_strategy=self.id)

    # Add live MACD state on the same side. This preserves cross-strategy coexistence semantics.
    with self.store.lock:
        for st in self.store.state.get("macd", {}).values():
            if not isinstance(st, dict) or str(st.get("symbol") or "").upper() != symbol:
                continue
            pos = st.get("position") or {}
            leg = pos.get("leg") or {}
            ps = str(pos.get("side") or leg.get("side") or "").upper()
            if pos and ps == side:
                reserved += bot.dec(leg.get("qty"))
    return max(bot.D(0), reserved)


bot.RangeEngine._other_strategy_reserved_qty = _other_strategy_reserved_qty_live_state


def _represented_leg_ids(reconciler):
    return reconciler._represented_state_leg_ids()


def _live_orders_for_owners(reconciler, snap, symbol, side, owners):
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


def _zero_or_resize_exact_lots(reconciler, updates, reason):
    """Transactionally set exact open_qty values, preserving rows/history."""
    if not updates:
        return
    ids = list(updates)
    placeholders = ",".join("?" for _ in ids)
    with reconciler.ledger.lock:
        reconciler.ledger.db.execute("BEGIN IMMEDIATE")
        rows = reconciler.ledger.db.execute(
            f"SELECT leg_id, open_qty FROM lots WHERE leg_id IN ({placeholders})", ids
        ).fetchall()
        current = {str(i): bot.dec(q) for i, q in rows}
        if set(current) != set(ids):
            reconciler.ledger.db.rollback()
            raise RuntimeError(f"ledger lot set changed/missing before commit: {current}")
        t = bot.now_ms()
        for leg_id, new_qty in updates.items():
            nq = bot.dec(new_qty)
            reconciler.ledger.db.execute(
                "UPDATE lots SET open_qty=?, closed_ms=CASE WHEN ?='0' THEN COALESCE(closed_ms, ?) ELSE NULL END WHERE leg_id=?",
                (str(nq), str(nq), t, leg_id),
            )
        reconciler.ledger.db.commit()
    bot.logger.warning(
        "RANGE OWNERSHIP LEDGER RESIZED | updates=%s | reason=%s | history_preserved=True",
        {k: str(v) for k, v in updates.items()}, reason,
    )


def _cancel_and_rebuild_range_protection(reconciler, strategy_id, st, symbol):
    b = st.get("basket") or {}
    legs = b.get("legs", []) or []
    if not legs:
        raise RuntimeError(f"active RANGE basket disappeared before protection rebuild: {strategy_id}")

    # Recovery basket uses basket-exit pairs; normal basket uses one native bracket.
    if int(b.get("alternations", 0) or 0) > 0 or b.get("native_basket_exit") or b.get("native_basket_stop"):
        tp = bot.dec(b.get("recovery_tp_price"))
        sl = bot.dec(b.get("recovery_stop_price"))
        if tp <= 0 or sl <= 0:
            raise RuntimeError(f"missing recovery targets for {strategy_id}: tp={tp} sl={sl}")
        reconciler.exe.cancel_basket_exit(symbol, b.get("native_basket_exit"))
        reconciler.exe.cancel_basket_exit(symbol, b.get("native_basket_stop"))
        b["native_basket_exit"] = reconciler.exe.install_basket_exit(strategy_id + ":TP", symbol, legs, tp, tp)
        b["native_basket_stop"] = reconciler.exe.install_basket_exit(strategy_id + ":SL", symbol, legs, sl, sl)
    else:
        if len(legs) != 1:
            raise RuntimeError(f"normal RANGE basket must have exactly one leg: {strategy_id} legs={len(legs)}")
        tp = bot.dec(b.get("tp_price"))
        sl = bot.dec(b.get("hard_stop_price"))
        if tp <= 0 or sl <= 0:
            raise RuntimeError(f"missing normal bracket targets for {strategy_id}: tp={tp} sl={sl}")
        reconciler.exe.cancel_bracket(symbol, b.get("native_bracket"))
        b["native_bracket"] = reconciler.exe.install_bracket(strategy_id, symbol, legs[0], tp, sl)

    reconciler.store.set_protection_block(strategy_id, None)
    st["last_update"] = bot.now_iso()
    reconciler.store.save()
    bot.logger.warning(
        "RANGE OWNERSHIP PROTECTION REBUILT | %s | %s | legs=%s",
        strategy_id, symbol, [(x.get("side"), x.get("qty"), x.get("id")) for x in legs],
    )


def repair_partial_reduction_stale_precedence(reconciler):
    """Repair only unambiguous one-live-RANGE-owner partial-reduction races."""
    if not bot.LIVE_TRADING:
        return []

    snap1 = reconciler.snapshot()
    physical = snap1.positions
    ledger_totals = reconciler.expected_by_symbol_side()
    state_totals = reconciler.expected_from_state_by_symbol_side()
    represented = _represented_leg_ids(reconciler)
    repaired = []

    for key in sorted(set(physical) | set(ledger_totals) | set(state_totals)):
        symbol, side = key
        if symbol not in reconciler.rules.rules or side not in ("LONG", "SHORT"):
            continue
        step = reconciler.rules.rules[symbol].step_size
        p = physical.get(key, bot.D(0))
        l = ledger_totals.get(key, bot.D(0))
        s = state_totals.get(key, bot.D(0))
        if p < step or l - p < step or p - s < step:
            continue

        lots = reconciler.ledger.open_lots_for_symbol_side(symbol, side)
        stale = []
        active = []
        for lot in lots:
            lid = str(lot.get("id") or "")
            sid = str(lot.get("strategy_id") or "")
            if not reconciler._is_current_range_grid_strategy(sid, symbol):
                # Any non-RANGE owner makes the attribution ambiguous.
                active = []
                stale = []
                break
            st = _state_for_range(reconciler.store, sid)
            if not isinstance(st, dict):
                active = []
                stale = []
                break
            if lid in represented:
                active.append((lot, st))
            else:
                if st.get("basket"):
                    active = []
                    stale = []
                    break
                if str(st.get("status") or "IDLE").upper() not in ("IDLE", "PROTECT"):
                    active = []
                    stale = []
                    break
                stale.append((lot, st))
        if not stale or not active:
            continue

        active_owners = {str(x[0].get("strategy_id") or "") for x in active}
        if len(active_owners) != 1:
            continue
        owner = next(iter(active_owners))
        st = _state_for_range(reconciler.store, owner)
        b = (st or {}).get("basket") or {}
        side_legs = [x for x in b.get("legs", []) or [] if str(x.get("side") or "").upper() == side]
        if len(side_legs) != 1:
            continue
        state_leg = side_legs[0]
        active_lot = next((x[0] for x in active if str(x[0].get("id") or "") == str(state_leg.get("id") or "")), None)
        if active_lot is None:
            continue

        stale_qty = sum((bot.dec(x[0].get("qty")) for x in stale), bot.D(0))
        active_ledger_qty = bot.dec(active_lot.get("qty"))
        current_state_qty = bot.dec(state_leg.get("qty"))

        # Unambiguous attribution proof:
        # after stale owners are removed, this single live owner can own exactly the full physical side;
        # physical must not exceed its durable active lot and state must not exceed physical.
        if active_ledger_qty + step < p or current_state_qty - p >= step:
            continue
        if abs((l - stale_qty) - active_ledger_qty) >= step:
            continue

        stale_owners = {str(x[0].get("strategy_id") or "") for x in stale}
        if _live_orders_for_owners(reconciler, snap1, symbol, side, stale_owners):
            continue

        bot.logger.warning(
            "RANGE PARTIAL REDUCTION OWNERSHIP CANDIDATE | %s %s | ledger=%s physical=%s state=%s "
            "stale=%s active_owner=%s active_ledger=%s active_state=%s",
            symbol, side, l, p, s, stale_qty, owner, active_ledger_qty, current_state_qty,
        )

        # Double snapshot before touching persistent ownership.
        snap2 = reconciler.snapshot()
        if abs(snap2.positions.get(key, bot.D(0)) - p) >= step:
            raise RuntimeError(f"physical changed during ownership verification {symbol} {side}")
        if abs(reconciler.expected_by_symbol_side().get(key, bot.D(0)) - l) >= step:
            raise RuntimeError(f"ledger changed during ownership verification {symbol} {side}")
        if abs(reconciler.expected_from_state_by_symbol_side().get(key, bot.D(0)) - s) >= step:
            raise RuntimeError(f"state changed during ownership verification {symbol} {side}")

        # Resize exact durable lots: stale -> 0; sole live owner -> current physical.
        updates = {str(x[0]["id"]): bot.D(0) for x in stale}
        updates[str(active_lot["id"])] = p
        _zero_or_resize_exact_lots(
            reconciler, updates,
            reason=f"stale_precedence_partial_reduction:{symbol}:{side}:owner={owner}:physical={p}",
        )

        # Restore the sole live state leg to the physical quantity. No accounting fields are touched.
        with reconciler.store.lock:
            st2 = _state_for_range(reconciler.store, owner)
            b2 = (st2 or {}).get("basket") or {}
            match = [x for x in b2.get("legs", []) or [] if str(x.get("id") or "") == str(active_lot["id"])]
            if len(match) != 1:
                raise RuntimeError(f"active state leg changed before state repair: {owner}")
            match[0]["qty"] = str(p)
            st2["last_update"] = bot.now_iso()
            reconciler.store.save()

        # Rebuild native protection for the corrected quantity before reopening entries.
        _cancel_and_rebuild_range_protection(reconciler, owner, st2, symbol)

        snap3 = reconciler.snapshot()
        p3 = snap3.positions.get(key, bot.D(0))
        l3 = reconciler.expected_by_symbol_side().get(key, bot.D(0))
        s3 = reconciler.expected_from_state_by_symbol_side().get(key, bot.D(0))
        if abs(p3 - p) >= step or abs(l3 - p3) >= step or abs(s3 - p3) >= step:
            raise RuntimeError(
                f"ownership repair did not converge {symbol} {side}: physical={p3} ledger={l3} state={s3}"
            )

        repaired.append((symbol, side, owner, p))
        bot.logger.warning(
            "RANGE PARTIAL REDUCTION OWNERSHIP VERIFIED | %s %s | owner=%s | physical=ledger=state=%s",
            symbol, side, owner, p3,
        )

    return repaired


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
        st = _state_for_range(reconciler.store, sid)
        if not isinstance(st, dict) or st.get("basket"):
            return []
        if str(st.get("status") or "IDLE").upper() not in ("IDLE", "PROTECT"):
            return []
    return stale


def repair_stale_closed_range_ownership(reconciler):
    if not bot.LIVE_TRADING:
        return []
    ledger = reconciler.expected_by_symbol_side()
    snap1 = reconciler.snapshot()
    state1 = reconciler.expected_from_state_by_symbol_side()
    repaired = []
    for key in sorted(set(ledger) | set(snap1.positions) | set(state1)):
        symbol, side = key
        if symbol not in reconciler.rules.rules or side not in ("LONG", "SHORT"):
            continue
        step = reconciler.rules.rules[symbol].step_size
        lq = ledger.get(key, bot.D(0)); pq = snap1.positions.get(key, bot.D(0)); sq = state1.get(key, bot.D(0))
        if abs(pq - sq) >= step or lq - pq < step:
            continue
        stale = _candidate_stale_lots(reconciler, symbol, side, pq, sq, lq)
        if not stale:
            continue
        owners = {str(x.get("strategy_id") or "") for x in stale}
        if _live_orders_for_owners(reconciler, snap1, symbol, side, owners):
            continue
        snap2 = reconciler.snapshot()
        if abs(snap2.positions.get(key, bot.D(0)) - pq) >= step:
            raise RuntimeError(f"physical changed during stale repair {symbol} {side}")
        stale2 = _candidate_stale_lots(
            reconciler, symbol, side, pq,
            reconciler.expected_from_state_by_symbol_side().get(key, bot.D(0)),
            reconciler.expected_by_symbol_side().get(key, bot.D(0)),
        )
        if sorted(str(x["id"]) for x in stale2) != sorted(str(x["id"]) for x in stale):
            raise RuntimeError(f"stale id set changed during verification {symbol} {side}")
        _zero_or_resize_exact_lots(
            reconciler, {str(x["id"]): bot.D(0) for x in stale2},
            reason=f"state_equals_physical_exact_ledger_excess:{symbol}:{side}",
        )
        l3 = reconciler.expected_by_symbol_side().get(key, bot.D(0))
        if abs(l3 - pq) >= step:
            raise RuntimeError(f"stale repair did not converge {symbol} {side}: ledger={l3} physical={pq}")
        repaired.append((symbol, side))
        bot.logger.warning("STALE CLOSED RANGE REPAIR VERIFIED | %s %s | physical=state=ledger=%s", symbol, side, pq)
    return repaired


def _reconcile_v63(self):
    try:
        repaired_partial = repair_partial_reduction_stale_precedence(self)
        repaired_stale = repair_stale_closed_range_ownership(self)
        if repaired_partial or repaired_stale:
            bot.logger.warning(
                "V63 OWNERSHIP REPAIR COMPLETE | partial=%s stale=%s",
                repaired_partial, repaired_stale,
            )
    except Exception as exc:
        reason = f"V63_OWNERSHIP_REPAIR_FAILED:{type(exc).__name__}:{exc}"
        self.store.set_trade_gate(False, reason)
        bot.logger.exception("V63 OWNERSHIP REPAIR FAIL-CLOSED | %s", reason)
        return False
    return _original_reconcile(self)


bot.Reconciler.reconcile = _reconcile_v63
bot.VERSION = f"{bot.VERSION}-stale-precedence-partial-reduction-v63"



# -----------------------------------------------------------------------------
# v64 OBSERVABILITY / PERFORMANCE METRICS (read-only; strategy behavior unchanged)
# -----------------------------------------------------------------------------
def _d64(v):
    try:
        return bot.dec(v)
    except Exception:
        return bot.D(0)


def _metrics_v64(store):
    """Build a compact, persistent-state performance snapshot without mutating state."""
    rows = []
    with store.lock:
        state = store.state
        for key, st in (state.get("range_grids", {}) or {}).items():
            if not isinstance(st, dict):
                continue
            symbol = str(st.get("symbol") or str(key).split(":")[0]).upper()
            gid = str(key).split(":")[-1]
            eq = _d64(st.get("equity", st.get("bankroll", 0)))
            base = bot.BTC_RANGE_GRID_BANKROLL_USD if symbol == "BTCUSDT" else bot.RANGE_GRID_BANKROLL_USD
            rows.append((f"R:{symbol}:{gid}", "RANGE", symbol, eq, base, _d64(st.get("recovery_deficit", 0)), int(st.get("loss_streak", st.get("recovery_failures", 0)) or 0), str(st.get("status") or "IDLE")))
        for key, st in (state.get("macd", {}) or {}).items():
            if not isinstance(st, dict):
                continue
            symbol = str(st.get("symbol") or str(key).split(":")[0]).upper()
            tf = str(st.get("timeframe") or str(key).split(":")[-1])
            eq = _d64(st.get("equity", st.get("bankroll", 0)))
            base = bot.BTC_INITIAL_BANKROLL_USD if symbol == "BTCUSDT" else bot.INITIAL_BANKROLL_USD
            rows.append((f"M:{symbol}:{tf}", "MACD", symbol, eq, base, _d64(st.get("recovery_deficit", 0)), int(st.get("loss_streak", 0) or 0), "OPEN" if st.get("position") else "FLAT"))
    by_engine = {}
    by_symbol = {}
    for sid, eng, sym, eq, base, rd, streak, status in rows:
        for bucket, name in ((by_engine, eng), (by_symbol, sym)):
            x = bucket.setdefault(name, {"equity": bot.D(0), "base": bot.D(0), "rd": bot.D(0), "units": 0})
            x["equity"] += eq; x["base"] += base; x["rd"] += rd; x["units"] += 1
    return rows, by_engine, by_symbol


def log_metrics_v64(store):
    rows, engines, symbols = _metrics_v64(store)
    def fmt(bucket):
        return " | ".join(f"{k}:eq={v['equity']},base={v['base']},net={v['equity']-v['base']},RD={v['rd']},units={v['units']}" for k,v in sorted(bucket.items()))
    bot.logger.info("PERFORMANCE METRICS V64 | engines=[%s] | symbols=[%s]", fmt(engines), fmt(symbols))
    bot.logger.info("STRATEGY METRICS V64 | %s", " | ".join(f"{sid}:eq={eq},base={base},net={eq-base},RD={rd},streak={streak},status={status}" for sid,eng,sym,eq,base,rd,streak,status in rows))


_original_heartbeat_v64 = getattr(bot.Bot, "heartbeat", None)
if _original_heartbeat_v64 is not None:
    def _heartbeat_metrics_v64(self, *args, **kwargs):
        out = _original_heartbeat_v64(self, *args, **kwargs)
        try:
            now = time.time()
            last = getattr(self, "_metrics_v64_last", 0.0)
            if now - last >= 300:
                self._metrics_v64_last = now
                log_metrics_v64(self.store)
        except Exception:
            bot.logger.exception("PERFORMANCE METRICS V64 ERROR")
        return out
    bot.Bot.heartbeat = _heartbeat_metrics_v64

bot.VERSION = f"{bot.VERSION}-metrics-v64"


def main():
    bot.logger.warning(
        "RANGE OWNERSHIP PRECEDENCE FIX ACTIVE | version=v63 | "
        "policy=live-state-reservation; stale-owner-first; exact-one-live-owner repair; no-market-order; metrics=v64-read-only"
    )
    runner.main()


if __name__ == "__main__":
    main()
