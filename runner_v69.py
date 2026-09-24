#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Perpetual Principal production entrypoint v69.

Repairs a proven state/ledger ownership loss without market orders.

Production failure fixed:
- ledger and physical ETH SHORT both contained 0.009;
- state represented only 0.002 (G1);
- the missing 0.007 was durably owned by RANGE:ETHUSDT:G2;
- G2 still had a live basket, so the older orphan repair correctly refused to
  liquidate/reconstruct it and the bot remained fail-closed.

v69 restores a missing RANGE leg into state ONLY when its identity is
unambiguous from durable ledger + current physical exposure:
1. ledger == physical for symbol/side;
2. every missing durable lot belongs to a current RANGE grid;
3. the owning grid already has a live basket;
4. that exact leg_id is absent from all state;
5. missing lots exactly equal ledger - state;
6. no unknown ownership is involved.

It copies only immutable lot identity/qty/entry into the owning basket, using
existing basket protection targets/state. It never sends/cancels market orders,
never changes physical positions, bankroll/equity/RD/risk parameters, and leaves
the existing fail-closed gate in place until the inherited reconciler proves
ledger == state == physical.
"""

import runner_v68 as base

bot = base.bot
_original_reconcile_v68 = bot.Reconciler.reconcile


def _restore_missing_live_range_lots_v69(self):
    ledger = self.expected_by_symbol_side()
    state = self.expected_from_state_by_symbol_side()
    snap = self.snapshot()
    physical = snap.positions
    represented = self._represented_state_leg_ids()
    repairs = []

    for key in set(ledger) | set(state):
        symbol, side = key
        lq = ledger.get(key, bot.D(0)); sq = state.get(key, bot.D(0)); pq = physical.get(key, bot.D(0))
        step = self.rules.rules[symbol].step_size
        missing = lq - sq
        if missing < step or abs(lq - pq) >= step:
            continue

        lots = self.ledger.open_lots_for_symbol_side(symbol, side)
        candidates = [x for x in lots if str(x.get("id") or "") not in represented]
        cq = sum((bot.dec(x.get("qty")) for x in candidates), bot.D(0))
        if not candidates or abs(cq - missing) >= step:
            continue

        planned = []
        safe = True
        with self.store.lock:
            for lot in candidates:
                sid = str(lot.get("strategy_id") or "")
                if not self._is_current_range_grid_strategy(sid, symbol):
                    safe = False; break
                st = self._range_state_for_strategy(sid)
                basket = (st or {}).get("basket") if isinstance(st, dict) else None
                if not basket or not isinstance(basket.get("legs"), list):
                    safe = False; break
                if any(str(x.get("id") or "") == str(lot.get("id") or "") for x in basket["legs"]):
                    safe = False; break
                # A live recovery basket must retain authoritative TP/SL targets.
                if int(basket.get("alternations", 0) or 0) > 0:
                    if bot.dec(basket.get("recovery_tp_price")) <= 0 or bot.dec(basket.get("recovery_stop_price")) <= 0:
                        safe = False; break
                planned.append((sid, st, basket, lot))
        if not safe:
            continue

        block = f"RANGE_STATE_LIVE_LOT_RESTORE:{symbol}:{side}"
        self.store.set_operational_block(block, f"v69 proof ledger=physical={lq} state={sq} missing={missing}")
        try:
            with self.store.lock:
                # Re-prove state absence immediately before mutation.
                current_ids = self._represented_state_leg_ids()
                if any(str(lot.get("id") or "") in current_ids for _,_,_,lot in planned):
                    raise RuntimeError("state changed before v69 commit")
                for sid, st, basket, lot in planned:
                    q = bot.dec(lot.get("qty")); ep = bot.dec(lot.get("entry_price"))
                    leg = {
                        "id": str(lot.get("id")),
                        "side": side,
                        "qty": str(q),
                        "entry_price": str(ep),
                        "signal_price": str(ep),
                        "price_source": "DURABLE_LEDGER_RESTORE_V69",
                        "notional": str(q * ep),
                        "opened_at": bot.now_iso(),
                        "reason": "STATE_LIVE_LOT_RESTORE_V69",
                    }
                    basket["legs"].append(leg)
                    st["last_update"] = bot.now_iso()
                    st["last_result"] = "STATE_LIVE_LOT_RESTORED_V69"
                    bot.logger.warning(
                        "RANGE LIVE LOT STATE RESTORE V69 | strategy=%s symbol=%s side=%s leg=%s qty=%s entry=%s | positions=UNTOUCHED orders=UNTOUCHED",
                        sid, symbol, side, leg["id"], q, ep,
                    )
                self.store.save()

            # State mutation is accepted only if it exactly closes the state gap.
            after = self.expected_from_state_by_symbol_side().get(key, bot.D(0))
            if abs(after - lq) >= step:
                raise RuntimeError(f"v69 post-state proof failed {key}: state={after} ledger={lq}")
            self.store.set_operational_block(block, None)
            repairs.append((symbol, side, missing))
        except Exception as exc:
            self.store.set_operational_block(block, f"V69_RESTORE_FAILED:{exc}")
            bot.logger.exception("RANGE LIVE LOT STATE RESTORE V69 FAILED | %s %s | %s", symbol, side, exc)
    return repairs


def _reconcile_v69(self, *args, **kwargs):
    ok = _original_reconcile_v68(self, *args, **kwargs)
    if ok:
        return True
    try:
        repaired = _restore_missing_live_range_lots_v69(self)
        if repaired:
            bot.logger.warning("RANGE LIVE LOT STATE RESTORE V69 | repairs=%s | invoking authoritative reconcile", repaired)
            return _original_reconcile_v68(self, *args, **kwargs)
    except Exception:
        bot.logger.exception("RANGE LIVE LOT STATE RESTORE V69 | fail_closed=True")
    return ok


bot.Reconciler.reconcile = _reconcile_v69
bot.VERSION = f"{bot.VERSION}-live-lot-state-restore-v69"


def main():
    bot.logger.warning(
        "RANGE LIVE LOT STATE RESTORE V69 ACTIVE | proof=ledger_equals_physical+exact_missing_durable_range_lots+live_owner_basket | "
        "positions=UNTOUCHED orders=UNTOUCHED accounting=PRESERVED risk=UNCHANGED fail_closed=True"
    )
    base.main()


if __name__ == "__main__":
    main()
