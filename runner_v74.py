#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Perpetual Principal production entrypoint v74.

V74 resolves a narrow operational deadlock introduced by V73 fail-closed
ownership protection: an unknown physical position on one symbol must not halt
unrelated symbols.

Safety rules:
- NEVER adopt the unknown position into ledger/state;
- NEVER close, reduce, cancel, or otherwise mutate the unknown position/orders;
- quarantine the affected symbol only;
- allow unaffected symbols to continue when the ONLY mismatch is
  physical > 0 while ledger == state == 0;
- if any other mismatch exists, preserve V73 global fail-closed behavior;
- persist only the quarantine metadata in /data state.
"""

import runner_v73 as base

bot = base.bot
_original_reconcile_v73 = bot.Reconciler.reconcile
_original_acquire_owner = bot.acquire_owner


def _physical_only_unknown(mismatches):
    if not mismatches:
        return False
    for (symbol, side), physical, ledger, state in mismatches:
        if physical <= 0 or ledger != 0 or state != 0:
            return False
    return True


def _collect_mismatches(self):
    ledger = self.expected_by_symbol_side()
    state = self.expected_from_state_by_symbol_side()
    physical = self.snapshot().positions
    keys = set(ledger) | set(state) | set(physical)
    out = []
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
            out.append((key, pq, lq, sq))
    return out


def _set_quarantine(self, mismatches):
    symbols = sorted({key[0] for key, *_ in mismatches})
    reason = "V74_UNKNOWN_PHYSICAL_POSITION"
    now = bot.now_iso()
    with self.store.lock:
        q = self.store.state.setdefault("ownership_quarantine", {})
        for symbol in symbols:
            q[symbol] = {
                "reason": reason,
                "at": now,
                "positions": [
                    {
                        "side": side,
                        "physical": str(physical),
                        "ledger": str(ledger),
                        "state": str(state),
                    }
                    for (sym, side), physical, ledger, state in mismatches
                    if sym == symbol
                ],
            }

        ks = self.store.state.get("kill_switch", {}) or {}
        gate = self.store.state.get("trade_gate", {}) or {}
        ks_reason = str(ks.get("reason") or "")
        gate_reason = str(gate.get("reason") or "")

        # Release ONLY the mismatch kill/gate created by V72/V73 for this exact
        # physical-only unknown-position condition. Other safety blocks remain.
        if ks.get("mode") in ("SOFT", "HARD") and (
            "POSITION_MISMATCH_LEDGER" in ks_reason
            or "V73_OWNERSHIP_GUARD" in ks_reason
        ):
            self.store.state["kill_switch"] = {
                "mode": "OFF",
                "reason": None,
                "at": now,
            }

        if (
            not gate_reason
            or "POSITION_MISMATCH_LEDGER" in gate_reason
            or "V73_OWNERSHIP_GUARD" in gate_reason
        ):
            self.store.state["trade_gate"] = {
                "open_allowed": True,
                "reason": None,
                "at": now,
            }

        self.store.save()

    bot.logger.warning(
        "V74 OWNERSHIP QUARANTINE | symbols=%s | unknown_physical=UNTOUCHED | "
        "ledger=UNTOUCHED state=UNTOUCHED orders=UNTOUCHED | unrelated_symbols=RELEASED",
        symbols,
    )
    return symbols


def _clear_quarantine_if_resolved(self):
    with self.store.lock:
        q = self.store.state.get("ownership_quarantine") or {}
        if not q:
            return

        # Remove quarantine only after a fresh authoritative snapshot proves
        # the quarantined symbol is physically flat. No position is modified.
        physical = self.snapshot().positions
        resolved = [
            symbol
            for symbol in q
            if not any(key[0] == symbol and qty > 0 for key, qty in physical.items())
        ]

        for symbol in resolved:
            q.pop(symbol, None)

        if resolved:
            self.store.save()
            bot.logger.warning(
                "V74 OWNERSHIP QUARANTINE CLEARED | symbols=%s | flat=CONFIRMED",
                resolved,
            )


def _reconcile_v74(self, *args, **kwargs):
    ok = _original_reconcile_v73(self, *args, **kwargs)
    mismatches = _collect_mismatches(self)

    if _physical_only_unknown(mismatches):
        _set_quarantine(self, mismatches)
        return True

    if not mismatches:
        _clear_quarantine_if_resolved(self)

    return bool(ok)


def _acquire_owner_v74(store, symbol, strategy_id):
    sym = str(symbol).upper()
    with store.lock:
        q = store.state.get("ownership_quarantine") or {}
        if sym in q:
            bot.logger.warning(
                "V74 ENTRY BLOCKED BY OWNERSHIP QUARANTINE | symbol=%s strategy=%s | "
                "unknown_physical=UNTOUCHED",
                sym,
                strategy_id,
            )
            return False

    return _original_acquire_owner(store, symbol, strategy_id)


bot.Reconciler.reconcile = _reconcile_v74
bot.acquire_owner = _acquire_owner_v74
bot.VERSION = f"{bot.VERSION}-scoped-ownership-quarantine-v74"


def main():
    bot.logger.warning(
        "OWNERSHIP QUARANTINE V74 ACTIVE | physical-only-unknown=scoped | "
        "automatic_adoption=DISABLED | market_orders=NONE | "
        "unknown_position=UNTOUCHED"
    )
    base.main()


if __name__ == "__main__":
    main()
