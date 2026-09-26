#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Perpetual Principal production entrypoint v73.

Safety hardening over v72.

Goals:
- never infer/adopt unknown physical exposure into the durable ledger;
- never remove ownership merely because state is missing;
- keep fail-closed behavior whenever physical/ledger/state diverge;
- preserve v72 native protection migration for already-owned RANGE baskets;
- do not send market orders as part of this migration layer.

This file intentionally does NOT repair an already-orphaned live position.
Existing orphan exposure remains blocked for explicit reconciliation.
"""

import runner_v72 as base

bot = base.bot

_original_reconcile_v72 = bot.Reconciler.reconcile


def _qty_map_state(self):
    """Return the reconciler's authoritative state quantities."""
    return self.expected_from_state_by_symbol_side()


def _ownership_guard_v73(self):
    """Fail closed unless physical, durable ledger and state agree."""
    ledger = self.expected_by_symbol_side()
    state = _qty_map_state(self)
    snap = self.snapshot()
    physical = snap.positions

    keys = set(ledger) | set(state) | set(physical)
    mismatches = []

    for key in sorted(keys):
        symbol, side = key
        lq = ledger.get(key, bot.D(0))
        sq = state.get(key, bot.D(0))
        pq = physical.get(key, bot.D(0))

        try:
            step = self.rules.rules[symbol].step_size
        except Exception:
            step = bot.D("0.00000001")

        tol = max(bot.D("0.00000001"), bot.dec(step))

        if abs(pq - lq) >= tol or abs(lq - sq) >= tol:
            mismatches.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "physical": str(pq),
                    "ledger": str(lq),
                    "state": str(sq),
                }
            )

    if not mismatches:
        return True

    reason = f"V73_OWNERSHIP_GUARD:{mismatches}"

    with self.store.lock:
        self.store.state["trade_gate"] = {
            "open_allowed": False,
            "reason": reason,
            "at": bot.now_iso(),
        }

    try:
        self.store.save()
    except Exception:
        bot.logger.exception(
            "V73 OWNERSHIP GUARD | gate persistence failed; in-memory gate remains closed"
        )

    bot.logger.error(
        "V73 OWNERSHIP GUARD | FAIL-CLOSED | mismatches=%s | "
        "automatic_adoption=DISABLED positions=UNTOUCHED orders=UNTOUCHED",
        mismatches,
    )
    return False


def _reconcile_v73(self, *args, **kwargs):
    """Run inherited authoritative reconciliation, then enforce triple equality."""
    ok = _original_reconcile_v72(self, *args, **kwargs)

    try:
        guard_ok = _ownership_guard_v73(self)
    except Exception as exc:
        reason = f"V73_OWNERSHIP_GUARD_ERROR:{type(exc).__name__}:{exc}"
        with self.store.lock:
            self.store.state["trade_gate"] = {
                "open_allowed": False,
                "reason": reason,
                "at": bot.now_iso(),
            }
        try:
            self.store.save()
        except Exception:
            pass
        bot.logger.exception(
            "V73 OWNERSHIP GUARD ERROR | fail_closed=True | %s", exc
        )
        return False

    return bool(ok and guard_ok)


bot.Reconciler.reconcile = _reconcile_v73
bot.VERSION = f"{bot.VERSION}-ownership-fail-closed-v73"


def main():
    bot.logger.warning(
        "OWNERSHIP GUARD V73 ACTIVE | proof=physical_equals_ledger_equals_state | "
        "automatic_orphan_adoption=DISABLED | market_orders=NONE | "
        "positions=UNTOUCHED | fail_closed=True"
    )
    base.main()


if __name__ == "__main__":
    main()
