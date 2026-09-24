#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Perpetual Principal production entrypoint v71.

Completes the v69/v70 ownership migration after partial fills.

Observed production state can legitimately shrink after a native fill. The
one-shot v70 protection order then becomes stale and the recovery basket may
still carry the pre-fill leg quantity. v71 performs a narrow migration before
each RANGE recovery tick:
- cancel ONLY legacy owner orders containing ':V70:' for that exact strategy;
- invoke the existing durable-ledger/physical ghost-leg reconciler;
- that reconciler rebuilds state quantities from exact ledger ownership capped
  by physical capacity and reinstalls native TP/SL at persisted basket targets.

No market order is sent. Other strategy orders, bankroll, RD, equity, anchors,
risk parameters and unrelated positions are untouched.
"""

import runner_v70 as base

bot = base.bot
_original_tick_v70 = bot.RangeEngine.tick
_cleaned = set()


def _cancel_stale_v70_orders(self):
    key = self.id
    # Repeat until none exist; normally one-shot. This is idempotent.
    found = False
    for o in self.client.open_orders(self.symbol) or []:
        cid = str(o.get("clientOrderId") or "")
        if not cid:
            continue
        owner = self.exe.ledger.order_owner(cid)
        if not owner or not str(owner).startswith(key + ":V70:"):
            continue
        found = True
        self.client.cancel_order(self.symbol, cid)
        bot.logger.warning(
            "RANGE V71 STALE V70 ORDER CANCELLED | strategy=%s cid=%s type=%s qty=%s | "
            "market_position=UNTOUCHED other_orders=UNTOUCHED",
            key, cid, o.get("type"), o.get("origQty"),
        )
    if found:
        # Prove none of this migration's stale orders remain.
        remain = []
        for o in self.client.open_orders(self.symbol) or []:
            cid = str(o.get("clientOrderId") or "")
            owner = self.exe.ledger.order_owner(cid) if cid else None
            if owner and str(owner).startswith(key + ":V70:"):
                remain.append(cid)
        if remain:
            raise RuntimeError(f"V71 stale V70 cancellation not confirmed: {remain}")
    _cleaned.add(key)


def _tick_v71(self, price):
    with self.store.lock:
        st = self.st()
        b = st.get("basket") if isinstance(st, dict) else None
        recovery = bool(b and int(b.get("alternations", 0) or 0) > 0)
    if recovery:
        try:
            if self.id not in _cleaned:
                _cancel_stale_v70_orders(self)
            # Existing production-safe reconciler: exact durable lot ids + physical cap,
            # and synchronous native protection rebuild if any quantity changed.
            changed = self._reconcile_range_ghost_legs(b, price)
            if changed:
                bot.logger.warning(
                    "RANGE V71 PARTIAL-FILL RECONCILE CONFIRMED | strategy=%s | "
                    "ledger/state/physical ownership resynchronized before tick",
                    self.id,
                )
        except Exception as exc:
            self.store.set_protection_block(self.id, f"V71_PARTIAL_FILL_RECONCILE_FAILED:{exc}")
            bot.logger.exception("RANGE V71 PARTIAL-FILL RECONCILE FAIL-CLOSED | %s", self.id)
            return
    return _original_tick_v70(self, price)


bot.RangeEngine.tick = _tick_v71
bot.VERSION = f"{bot.VERSION}-partial-fill-ownership-v71"


def main():
    bot.logger.warning(
        "RANGE PARTIAL-FILL OWNERSHIP V71 ACTIVE | stale_scope=:V70:ONLY | "
        "rebuild=durable_ledger+physical_capacity | market_orders=NONE | fail_closed=True"
    )
    base.main()


if __name__ == "__main__":
    main()
