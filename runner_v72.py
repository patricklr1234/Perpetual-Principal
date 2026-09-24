#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Perpetual Principal production entrypoint v72.

Finalizes v71 migration by atomically rebuilding native protection after stale
v70 orders are removed. Uses current durable/state basket quantities and the
persisted recovery TP/SL targets. No market order is sent.
"""

import runner_v71 as base

bot = base.bot
_done = set()


def _migrate_and_protect_v72(self, price):
    st = self.st()
    b = st.get("basket") if isinstance(st, dict) else None
    if not b or int(b.get("alternations", 0) or 0) <= 0:
        return
    if self.id in _done:
        return

    block = f"V72_ATOMIC_NATIVE_REBUILD:{self.id}"
    self.store.set_operational_block(block, "removing stale v70 orders and rebuilding current basket protection")
    try:
        # Remove only migration orders from v70.
        for o in self.client.open_orders(self.symbol) or []:
            cid = str(o.get("clientOrderId") or "")
            owner = self.exe.ledger.order_owner(cid) if cid else None
            if owner and str(owner).startswith(self.id + ":V70:"):
                self.client.cancel_order(self.symbol, cid)

        # Reconcile current leg quantities from durable ownership/physical first.
        self._reconcile_range_ghost_legs(b, price)

        # Cancel only metadata-owned old basket exits, then rebuild both sides.
        self.exe.cancel_basket_exit(self.symbol, b.get("native_basket_exit"))
        self.exe.cancel_basket_exit(self.symbol, b.get("native_basket_stop"))
        b["native_basket_exit"] = None
        b["native_basket_stop"] = None

        tp = bot.dec(b.get("recovery_tp_price"))
        sl = bot.dec(b.get("recovery_stop_price"))
        if tp <= 0 or sl <= 0 or not b.get("legs"):
            raise RuntimeError("V72 missing recovery targets/legs")

        b["native_basket_exit"] = self.exe.install_basket_exit(
            self.id + ":TP", self.symbol, b["legs"], tp, price)
        b["native_basket_stop"] = self.exe.install_basket_exit(
            self.id + ":SL", self.symbol, b["legs"], sl, price)
        if not b["native_basket_exit"] or not b["native_basket_stop"]:
            raise RuntimeError("V72 native protection confirmation missing")

        self.store.set_protection_block(self.id, None)
        self.store.set_operational_block(block, None)
        st["last_update"] = bot.now_iso()
        self.store.save()
        _done.add(self.id)
        bot.logger.warning(
            "RANGE V72 ATOMIC NATIVE REBUILD CONFIRMED | strategy=%s qtys=%s TP=%s SL=%s | "
            "market_position=UNTOUCHED bankroll=UNTOUCHED RD=UNTOUCHED",
            self.id, [(x.get("side"), x.get("qty")) for x in b["legs"]], tp, sl)
    except Exception as exc:
        self.store.set_protection_block(self.id, f"V72_NATIVE_REBUILD_FAILED:{exc}")
        self.store.set_operational_block(block, f"V72_NATIVE_REBUILD_FAILED:{exc}")
        bot.logger.exception("RANGE V72 ATOMIC NATIVE REBUILD FAIL-CLOSED | %s", self.id)
        raise


def _tick_v72(self, price):
    with self.store.lock:
        st = self.st()
        b = st.get("basket") if isinstance(st, dict) else None
        recovery = bool(b and int(b.get("alternations", 0) or 0) > 0)
    if recovery and self.id not in _done:
        try:
            _migrate_and_protect_v72(self, price)
        except Exception:
            return
    # Bypass v71's transitional cancellation routine; use original v70 tick.
    return base._original_tick_v70(self, price)


bot.RangeEngine.tick = _tick_v72
bot.VERSION = f"{bot.VERSION}-atomic-native-rebuild-v72"


def main():
    bot.logger.warning("RANGE ATOMIC NATIVE REBUILD V72 ACTIVE | stale_v70_cleanup+current_qty_TP_SL | market_orders=NONE | fail_closed=True")
    base.base.main()


if __name__ == "__main__":
    main()
