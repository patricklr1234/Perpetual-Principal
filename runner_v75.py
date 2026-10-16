#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Perpetual Principal production entrypoint v75.

Completes the durable-ledger -> state projection repair before the V74
ownership guard.  It never changes exchange positions or open orders.
"""

import os
import shutil
from pathlib import Path


def _recover_log_storage():
    bot_dir = Path(os.getenv("BOT_DIR", "/data"))
    try:
        if shutil.disk_usage(bot_dir).free < 64 * 1024 * 1024:
            try:
                (bot_dir / "aster_bot.log").write_text("", encoding="utf-8")
            except Exception:
                pass
            for path in bot_dir.glob("aster_bot.log.*"):
                try:
                    path.unlink()
                except Exception:
                    pass
    except Exception:
        pass


_recover_log_storage()

import runner_v74 as base  # noqa: E402
import runner_v69 as repair_base  # noqa: E402

bot = base.bot
_original_reconcile_v74 = bot.Reconciler.reconcile


def _exact_owned_state_restore(self):
    ledger = self.expected_by_symbol_side()
    state = self.expected_from_state_by_symbol_side()
    physical = self.snapshot().positions
    represented = self._represented_state_leg_ids()
    repairs = []

    for key in sorted(set(ledger) | set(state) | set(physical)):
        symbol, side = key
        lq = ledger.get(key, bot.D(0))
        sq = state.get(key, bot.D(0))
        pq = physical.get(key, bot.D(0))
        step = self.rules.rules[symbol].step_size
        if lq <= 0 or abs(lq - pq) >= step or abs(lq - sq) < step:
            continue

        missing = lq - sq
        lots = self.ledger.open_lots_for_symbol_side(symbol, side)
        candidates = [x for x in lots if str(x.get("id") or "") not in represented]
        if not candidates:
            continue
        if abs(sum((bot.dec(x.get("qty")) for x in candidates), bot.D(0)) - missing) >= step:
            continue

        planned = []
        safe = True
        with self.store.lock:
            for lot in candidates:
                sid = str(lot.get("strategy_id") or "")
                if not self._is_current_range_grid_strategy(sid, symbol):
                    safe = False
                    break
                st = self._range_state_for_strategy(sid)
                basket = st.get("basket") if isinstance(st, dict) else None
                if not isinstance(basket, dict) or not isinstance(basket.get("legs"), list):
                    safe = False
                    break
                if any(str(x.get("id") or "") == str(lot.get("id") or "") for x in basket["legs"]):
                    safe = False
                    break
                planned.append((sid, st, basket, lot))

        if not safe or not planned:
            continue

        block = f"RANGE_STATE_EXACT_OWNED_RESTORE:{symbol}:{side}"
        self.store.set_operational_block(block, f"ledger=physical={lq} state={sq} missing={missing}")
        try:
            with self.store.lock:
                if any(str(lot.get("id") or "") in self._represented_state_leg_ids() for _, _, _, lot in planned):
                    raise RuntimeError("state changed before exact restore")
                for sid, st, basket, lot in planned:
                    q = bot.dec(lot.get("qty"))
                    ep = bot.dec(lot.get("entry_price"))
                    basket["legs"].append({
                        "id": str(lot.get("id")),
                        "side": side,
                        "qty": str(q),
                        "entry_price": str(ep),
                        "signal_price": str(ep),
                        "price_source": "DURABLE_LEDGER_RESTORE_EXACT",
                        "notional": str(q * ep),
                        "opened_at": bot.now_iso(),
                        "reason": "STATE_EXACT_OWNED_RESTORE",
                    })
                    st["last_update"] = bot.now_iso()
                    st["last_result"] = "STATE_EXACT_OWNED_RESTORED"
                self.store.save()
            after = self.expected_from_state_by_symbol_side().get(key, bot.D(0))
            if abs(after - lq) >= step:
                raise RuntimeError(f"post-restore proof failed: state={after} ledger={lq}")
            self.store.set_operational_block(block, None)
            repairs.append((symbol, side, missing))
            bot.logger.warning(
                "EXACT OWNED STATE RESTORE | repairs=%s | ledger=physical=state | positions=UNTOUCHED orders=UNTOUCHED",
                repairs,
            )
        except Exception as exc:
            self.store.set_operational_block(block, f"EXACT_RESTORE_FAILED:{exc}")
            bot.logger.exception("EXACT OWNED STATE RESTORE FAILED | %s %s | %s", symbol, side, exc)

    return repairs


def _reconcile_v75(self, *args, **kwargs):
    try:
        repaired = _exact_owned_state_restore(self)
        if not repaired:
            repaired = repair_base._restore_missing_live_range_lots_v69(self)
        if repaired:
            bot.logger.warning("STATE PROJECTION REPAIR COMPLETE | repairs=%s | invoking authoritative reconcile", repaired)
    except Exception as exc:
        bot.logger.exception("STATE PROJECTION REPAIR FAILED | fail_closed=True | %s", exc)
    return _original_reconcile_v74(self, *args, **kwargs)


bot.Reconciler.reconcile = _reconcile_v75
bot.VERSION = f"{bot.VERSION}-exact-owned-state-restore-v75"


def main():
    bot.logger.warning(
        "EXACT OWNED STATE RESTORE V75 ACTIVE | proof=ledger_equals_physical+exact_durable_range_lots+existing_owner_basket | positions=UNTOUCHED orders=UNTOUCHED accounting=PRESERVED fail_closed=True"
    )
    base.main()


if __name__ == "__main__":
    main()
