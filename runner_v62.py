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

import time
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
    side = str(position_side).upper()
    symbol = str(self.symbol).upper()
    reserved = _live_range_state_qty(self.store, symbol, side, exclude_strategy=self.id)
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
                "UPDATE lots SET open_qty=?, updated_ms=? WHERE leg_id=?", (str(nq), t, leg_id)
            )
        reconciler.ledger.db.commit()
    bot.logger.warning("RANGE LEDGER OWNERSHIP REPAIR | updates=%s | reason=%s", updates, reason)


def _repair_unambiguous_stale_range_ownership(reconciler, snap):
    represented = _represented_leg_ids(reconciler)
    with reconciler.store.lock:
        grids = dict(reconciler.store.state.get("range_grids", {}))
    by_symbol_side = {}
    for key, st in grids.items():
        if not isinstance(st, dict):
            continue
        sid = str(st.get("strategy") or f"RANGE:{key}")
        symbol = str(st.get("symbol") or str(key).split(":")[0]).upper()
        basket = st.get("basket") or {}
        for leg in basket.get("legs", []) or []:
            side = str(leg.get("side") or "").upper()
            qty = bot.dec(leg.get("qty"))
            if side in ("LONG", "SHORT") and qty > 0:
                by_symbol_side.setdefault((symbol, side), []).append((sid, st, leg, qty))
    for (symbol, side), live in by_symbol_side.items():
        if len(live) != 1:
            continue
        sid, st, leg, state_qty = live[0]
        physical = bot.dec((snap.positions or {}).get((symbol, side), 0))
        if physical <= 0 or physical > state_qty:
            continue
        rows = reconciler.ledger.db.execute(
            "SELECT leg_id, strategy_id, open_qty FROM lots WHERE symbol=? AND position_side=? AND CAST(open_qty AS REAL)>0",
            (symbol, side),
        ).fetchall()
        live_rows = [(str(i), str(s), bot.dec(q)) for i, s, q in rows]
        stale = [(i, s, q) for i, s, q in live_rows if s.startswith("RANGE:") and i not in represented and s != sid]
        own = [(i, s, q) for i, s, q in live_rows if s == sid]
        if not stale or len(own) != 1:
            continue
        orders = _live_orders_for_owners(reconciler, snap, symbol, side, {s for _, s, _ in stale})
        if orders:
            continue
        own_id, _, own_qty = own[0]
        updates = {i: bot.D(0) for i, _, _ in stale}
        updates[own_id] = physical
        _zero_or_resize_exact_lots(reconciler, updates, "unambiguous stale closed RANGE owner")
        leg["qty"] = str(physical)
        with reconciler.store.lock:
            reconciler.store.save()
        bot.logger.warning("RANGE STATE OWNERSHIP REPAIR | strategy=%s symbol=%s side=%s qty=%s", sid, symbol, side, physical)
        return True
    return False


def _reconcile_v63(self, startup=False):
    ok = _original_reconcile(self, startup=startup)
    if ok:
        return ok
    try:
        snap = self.account.snapshot()
        if _repair_unambiguous_stale_range_ownership(self, snap):
            return _original_reconcile(self, startup=startup)
    except Exception:
        bot.logger.exception("RANGE OWNERSHIP REPAIR FAILED")
    return ok


bot.Reconciler.reconcile = _reconcile_v63


def _d64(v):
    try:
        return bot.dec(v)
    except Exception:
        return bot.D(0)


def _metrics_v64(store):
    rows = []
    with store.lock:
        for key, st in (store.state.get("range_grids", {}) or {}).items():
            if not isinstance(st, dict):
                continue
            symbol = str(st.get("symbol") or str(key).split(":")[0]).upper()
            eq = _d64(st.get("equity", st.get("bankroll", 0)))
            base = bot.BTC_INITIAL_BANKROLL_USD if symbol == "BTCUSDT" else bot.INITIAL_BANKROLL_USD
            rows.append((f"R:{key}", "RANGE", symbol, eq, base, _d64(st.get("recovery_deficit", 0)), int(st.get("recovery_failures", 0) or 0), str(st.get("status") or "")))
        for key, st in (store.state.get("macd", {}) or {}).items():
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
    bot.main()


if __name__ == "__main__":
    main()
