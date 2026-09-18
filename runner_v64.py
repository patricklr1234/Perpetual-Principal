#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Perpetual Principal production entrypoint v64.

Adds recurring, fail-closed cleanup for stale RANGE protection blocks created by
an ownership-repair protection rebuild failure. A block is cleared only when:
- the normal reconciler has already returned success (ledger/state/physical agree);
- the exact RANGE grid has no logical live quantity;
- the exact strategy owns no durable open lot on LONG or SHORT;
- the exact strategy owns no active exchange-native order;
- the persisted protection-block reason contains RANGE_LEDGER_STATE_REPAIR_PROTECTION_FAILED.

No market order, position, bankroll, PnL, recovery deficit, anchor or strategy
parameter is changed by this repair.
"""

import runner_v62 as base

bot = base.bot
MARKER = "RANGE_LEDGER_STATE_REPAIR_PROTECTION_FAILED"
_original_reconcile_v63 = bot.Reconciler.reconcile


def _range_state_for_strategy(reconciler, strategy_id):
    parts = str(strategy_id).split(":")
    if len(parts) != 3 or parts[0] != "RANGE":
        return None, None, None
    symbol = parts[1].upper()
    grid_id = parts[2]
    key = f"{symbol}:{grid_id}"
    st = (reconciler.store.state.get("range_grids", {}) or {}).get(key)
    return symbol, key, st


def _range_state_qty(st):
    if not isinstance(st, dict):
        return bot.D(0)
    basket = st.get("basket") or {}
    return sum(
        (bot.dec(x.get("qty")) for x in (basket.get("legs") or []) if isinstance(x, dict)),
        bot.D(0),
    )


def _strategy_ledger_qty(reconciler, strategy_id, symbol):
    return (
        reconciler.ledger.open_strategy_qty(strategy_id, symbol, "LONG")
        + reconciler.ledger.open_strategy_qty(strategy_id, symbol, "SHORT")
    )


def _strategy_has_active_order(reconciler, strategy_id, symbol):
    rows = reconciler.client.open_orders(symbol)
    if not isinstance(rows, list):
        raise RuntimeError(f"openOrders indeterminado para {symbol}: {rows!r}")
    for row in rows:
        if str(row.get("status") or "NEW").upper() not in ("NEW", "PARTIALLY_FILLED"):
            continue
        cid = str(row.get("clientOrderId") or row.get("origClientOrderId") or "")
        if not cid:
            # Unknown active order means we cannot prove the target strategy is safe to release.
            raise RuntimeError(f"ordem ativa sem clientOrderId em {symbol}")
        owner = reconciler.ledger.order_owner(cid)
        if owner and (owner == strategy_id or str(owner).startswith(strategy_id + ":")):
            return True
    return False


def clear_proven_flat_range_repair_blocks(reconciler):
    if not bot.LIVE_TRADING:
        return []

    with reconciler.store.lock:
        blocks = dict(reconciler.store.state.get("protection_blocks", {}) or {})

    candidates = [
        (str(strategy_id), str(reason or ""))
        for strategy_id, reason in blocks.items()
        if str(strategy_id).startswith("RANGE:") and MARKER in str(reason or "").upper()
    ]
    if not candidates:
        return []

    cleared = []
    for strategy_id, reason in candidates:
        try:
            symbol, key, st = _range_state_for_strategy(reconciler, strategy_id)
            if not symbol or not isinstance(st, dict):
                continue
            if _range_state_qty(st) > 0:
                continue
            if _strategy_ledger_qty(reconciler, strategy_id, symbol) > 0:
                continue
            if _strategy_has_active_order(reconciler, strategy_id, symbol):
                continue

            # Double-check target state/ledger after the exchange order query.
            with reconciler.store.lock:
                st2 = (reconciler.store.state.get("range_grids", {}) or {}).get(key)
                current_reason = str(
                    (reconciler.store.state.get("protection_blocks", {}) or {}).get(strategy_id) or ""
                )
            if not isinstance(st2, dict) or _range_state_qty(st2) > 0:
                continue
            if _strategy_ledger_qty(reconciler, strategy_id, symbol) > 0:
                continue
            if MARKER not in current_reason.upper():
                continue

            reconciler.store.set_protection_block(strategy_id, None)
            cleared.append(strategy_id)
            bot.logger.warning(
                "RANGE STALE FLAT PROTECTION BLOCK CLEARED | strategy=%s | symbol=%s | "
                "reconcile=OK state_flat=True ledger_flat=True native_orders=False | accounting=PRESERVED",
                strategy_id,
                symbol,
            )
        except Exception as exc:
            bot.logger.warning(
                "RANGE STALE FLAT PROTECTION BLOCK | KEEP FAIL-CLOSED | strategy=%s | %s",
                strategy_id,
                exc,
            )

    return cleared


def _reconcile_v64(self):
    ok = _original_reconcile_v63(self)
    if not ok:
        return False
    cleared = clear_proven_flat_range_repair_blocks(self)
    if cleared:
        bot.logger.warning(
            "V64 RANGE FLAT BLOCK CLEANUP COMPLETE | cleared=%s | no_position_or_order_mutation=True",
            cleared,
        )
    return True


bot.Reconciler.reconcile = _reconcile_v64
bot.VERSION = f"{bot.VERSION}-range-flat-repair-block-runtime-clear-v64"


def main():
    bot.logger.warning(
        "RANGE FLAT REPAIR-BLOCK CLEANUP ACTIVE | version=v64 | marker=%s | "
        "policy=RECONCILE_OK+EXACT_STRATEGY_FLAT+NO_NATIVE_ORDER",
        MARKER,
    )
    base.main()


if __name__ == "__main__":
    main()
