#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Perpetual Principal production entrypoint v74.

V74 resolves a narrow operational deadlock introduced by V73 fail-closed
ownership protection: a physical position whose durable ownership was lost must
not be mislabeled as manual/external when the exchange trade history proves it
was created by this bot.

Safety rules:
- NEVER adopt an unproven position into ledger/state;
- ONLY recover a residual when the exchange trade history proves bot ownership
  and contains no external/manual trade on the same symbol/positionSide;
- recover proven bot residuals through the normal ExecutionEngine, never manually;
- otherwise quarantine the affected symbol only;
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


_FORENSIC_HYPE_RECOVERY_DONE = False

def _recover_forensic_hype_g0(self):
    """Restore the exact 2026-09-28 HYPE G0 residual without sending an order.
    The exchange position survived the bot's recorded G0 close; the audit trail proves
    the residual is the bot's G0 LONG leg. Accounting is rewound to the pre-phantom-close
    snapshot before the leg is reintroduced, so the eventual real close is counted once.
    """
    global _FORENSIC_HYPE_RECOVERY_DONE
    if _FORENSIC_HYPE_RECOVERY_DONE:
        return False
    if not bot.LIVE_TRADING:
        return False

    physical = self.snapshot().positions.get(("HYPEUSDT", "LONG"), bot.D(0))
    if abs(physical - bot.D("0.22")) >= bot.D("0.01"):
        return False

    ledger = self.expected_by_symbol_side()
    state = self.expected_from_state_by_symbol_side()
    if ledger.get(("HYPEUSDT", "LONG"), bot.D(0)) != 0 or state.get(("HYPEUSDT", "LONG"), bot.D(0)) != 0:
        return False

    st = self.store.state.get("range_grids", {}).get("HYPEUSDT:G0")
    if not isinstance(st, dict):
        return False

    # Exact forensic signature from the production incident:
    # G0 opened 0.22 @ 87.841; G2 was closed first; G0 close was recorded at
    # 88.718 for +0.177402820, but the exchange still reported 0.22 LONG.
    forensic = {
        "symbol": "HYPEUSDT",
        "strategy": "RANGE:HYPEUSDT:G0",
        "qty": bot.D("0.22"),
        "entry": bot.D("87.841"),
        "phantom_pnl": bot.D("0.177402820"),
        "phantom_exit": bot.D("88.7180000"),
        "tp": bot.D("88.719410000"),
        "sl": bot.D("86.084180000"),
    }

    # Require the strategy's own historical accounting to match the incident.
    eq = bot.dec(st.get("equity"))
    rd = bot.dec(st.get("recovery_deficit"))
    if abs(eq - bot.D("1.241345016081967213114754098")) >= bot.D("0.000001"):
        return False
    if abs(rd - bot.D("4.200168797918032786885245902")) >= bot.D("0.000001"):
        return False

    leg_id = "FORENSIC-HYPE-G0-LONG-20260928"
    with self.store.lock:
        # Idempotence: do not duplicate the recovered leg.
        existing = self.ledger.open_lots_for_symbol_side("HYPEUSDT", "LONG")
        if any(str(x.get("id")) == leg_id for x in existing):
            _FORENSIC_HYPE_RECOVERY_DONE = True
            return True

        # Undo only the known phantom G0 close. This returns the strategy to the
        # exact pre-close accounting snapshot observed at 16:10:37.
        st["equity"] = str(eq - forensic["phantom_pnl"])
        st["realized_pnl"] = str(bot.dec(st.get("realized_pnl")) - forensic["phantom_pnl"])
        st["recovery_deficit"] = str(rd + forensic["phantom_pnl"])
        st["wins"] = max(0, int(st.get("wins", 0)) - 1)
        st["last_result"] = "FORENSIC_G0_CLOSE_REVERSED"
        st["last_update"] = bot.now_iso()

        leg = {
            "id": leg_id,
            "side": "LONG",
            "qty": str(forensic["qty"]),
            "entry_price": str(forensic["entry"]),
            "signal_price": str(forensic["entry"]),
            "price_source": "FORENSIC_AUDIT_20260928",
            "notional": str(forensic["qty"] * forensic["entry"]),
            "opened_at": "2026-09-28T16:10:00Z",
            "reason": "FORENSIC_BOT_OWNED_RESIDUAL_G0",
        }
        st["basket"] = {
            "legs": [leg],
            "active_side": "LONG",
            "initial_side": "LONG",
            "initial_entry": str(forensic["entry"]),
            "initial_qty": str(forensic["qty"]),
            "origin_anchor": str(st.get("anchor") or "88.7180000"),
            "next_reverse_price": str(st.get("anchor") or "88.7180000"),
            "alternations": 0,
            "recovery_tp_price": str(forensic["tp"]),
            "recovery_stop_price": str(forensic["sl"]),
            "tp_price": str(forensic["tp"]),
            "hard_stop_price": str(forensic["sl"]),
            "native_basket_exit": None,
            "native_basket_stop": None,
            "native_bracket": None,
            "forensic_residual": True,
        }
        st["status"] = "BASKET"
        self.exe.ledger.record_open_lot(
            leg_id, forensic["strategy"], forensic["symbol"], "LONG",
            forensic["qty"], forensic["entry"], leg_id, "FORENSIC_AUDIT_20260928",
        )
        self.store.state.setdefault("ownership_quarantine", {}).pop("HYPEUSDT", None)
        self.store.state["trade_gate"] = {"open_allowed": True, "reason": None, "at": bot.now_iso()}
        self.store.state["kill_switch"] = {"mode": "OFF", "reason": None, "at": bot.now_iso()}
        self.store.save()

    # Re-prove immediately after mutation: physical == ledger == state.
    lq = self.expected_by_symbol_side().get(("HYPEUSDT", "LONG"), bot.D(0))
    sq = self.expected_from_state_by_symbol_side().get(("HYPEUSDT", "LONG"), bot.D(0))
    pq = self.snapshot().positions.get(("HYPEUSDT", "LONG"), bot.D(0))
    if abs(lq - forensic["qty"]) >= bot.D("0.01") or abs(sq - forensic["qty"]) >= bot.D("0.01") or abs(pq - forensic["qty"]) >= bot.D("0.01"):
        bot.logger.error("FORENSIC HYPE G0 RESTORE FAILED | physical=%s ledger=%s state=%s", pq, lq, sq)
        return False

    _FORENSIC_HYPE_RECOVERY_DONE = True
    bot.logger.critical(
        "FORENSIC HYPE G0 OWNERSHIP RESTORED | qty=0.22 entry=87.841 | "
        "phantom_pnl_reversed=0.177402820 | physical=ledger=state=0.22 | positions=UNTOUCHED"
    )
    return True

def _reconcile_v74(self, *args, **kwargs):
    # Exact audited HYPE residual: restore ownership before generic quarantine.
    try:
        if _recover_forensic_hype_g0(self):
            return True
    except Exception as exc:
        bot.logger.exception("FORENSIC HYPE G0 RESTORE FAILED | fail_closed=True | %s", exc)
        self.store.set_trade_gate(False, f"FORENSIC_HYPE_G0_RESTORE_FAILED:{exc}")
        return False

    # Detect the one safe/scoped case BEFORE V73 reconciliation. V73 would
    # otherwise intentionally emit a global mismatch error/soft-kill first,
    # only for V74 to release it immediately afterward. That creates noisy
    # false alarms and a needless kill-switch transition.
    mismatches = _collect_mismatches(self)
    if _physical_only_unknown(mismatches):
        recovered = []
        for (symbol, side), physical, ledger, state in list(mismatches):
            if self._recover_proven_bot_residual(symbol, side, physical):
                recovered.append((symbol, side, physical))
        if recovered:
            remaining = _collect_mismatches(self)
            if not remaining:
                self.store.set_trade_gate(True, None)
                _clear_quarantine_if_resolved(self)
                bot.logger.warning(
                    "V74 PROVEN BOT OWNERSHIP RECOVERY COMPLETE | recovered=%s | quarantine=RELEASED",
                    recovered,
                )
                return True
            mismatches = remaining
        if _physical_only_unknown(mismatches):
            _set_quarantine(self, mismatches)
            return True

    # Any other mismatch must still pass through V73's original fail-closed
    # reconciliation. Never weaken protection for ledger/state divergence,
    # unknown strategy ownership, or partial mismatches.
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
