#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Perpetual Principal production entrypoint v75.

Completes the deterministic reconciliation path before the V74 ownership guard:
- repair proven RANGE state orphans through the existing safe reconciler routine;
- strengthen bot-ownership proof with the durable order journal when Aster userTrades
  omit clientOrderId, without adopting unproven exposure;
- then run the original V74 proven-residual recovery/quarantine logic.

No manual exchange action is performed by this wrapper.
"""

import json
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

bot = base.bot
_original_reconcile_v74 = bot.Reconciler.reconcile
_original_prove_residual_v74 = bot.Reconciler._prove_bot_owned_residual


def _journal_bot_order_ids():
    """Read only durable order-journal provenance; never mutates the journal."""
    path = Path(os.getenv("BOT_DIR", "/data")) / "order_journal.jsonl"
    client_ids = set()
    order_ids = set()
    try:
        if not path.exists():
            return client_ids, order_ids
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue
                strategy = str(ev.get("strategy") or ev.get("strategy_id") or "")
                cid = str(ev.get("clientOrderId") or ev.get("client_order_id") or ev.get("cid") or "")
                oid = str(ev.get("orderId") or ev.get("order_id") or "")
                bot_owned = strategy.startswith(("RANGE:", "MACD:", "RECOVERY:", "PROVEN_BOT_RESIDUAL:")) or cid.startswith("a3-")
                if bot_owned:
                    if cid:
                        client_ids.add(cid)
                    if oid:
                        order_ids.add(oid)
    except Exception as exc:
        bot.logger.warning("OWNERSHIP JOURNAL INDEX FAIL | %s", exc)
    return client_ids, order_ids


def _prove_bot_owned_residual_v75(self, symbol, side, physical_qty):
    """Stronger proof: every relevant exchange trade must be attributable to the bot.

    Exact net quantity is preferred, but it is not the only ownership proof. If Aster
    omits clientOrderId on one or more fills, a matching durable order-journal orderId/
    clientOrderId is sufficient provenance. Quantity mismatch is treated as an accounting
    defect, not as evidence of manual ownership, provided no external trade exists.
    """
    try:
        if _original_prove_residual_v74(self, symbol, side, physical_qty):
            return True
    except Exception as exc:
        bot.logger.warning("OWNERSHIP BASE PROOF ERROR | %s %s | %s", symbol, side, exc)

    symbol = str(symbol).upper()
    side = str(side).upper()
    step = self.rules.rules[symbol].step_size if symbol in self.rules.rules else bot.D("0.00000001")
    if physical_qty < step:
        return False

    try:
        start_ms = bot.now_ms() - 14 * 24 * 60 * 60 * 1000
        rows = self.client.user_trades(symbol, start_ms=start_ms, limit=1000)
        if not isinstance(rows, list) or not rows:
            return False
        journal_cids, journal_oids = _journal_bot_order_ids()
        relevant = []
        unknown = []
        bot_net = bot.D(0)
        for row in rows:
            if str(row.get("positionSide") or "").upper() != side:
                continue
            qty = abs(bot.dec(row.get("qty") or row.get("executedQty")))
            if qty <= 0:
                continue
            cid = str(row.get("clientOrderId") or row.get("origClientOrderId") or "")
            oid = str(row.get("orderId") or "")
            owner = self.ledger.order_owner(cid) if cid else None
            if owner is None and oid:
                owner = self.ledger.order_owner_by_order_id(oid)
            bot_owned = (
                cid.startswith("a3-")
                or (owner is not None and str(owner).startswith(("RANGE:", "MACD:", "RECOVERY:")))
                or cid in journal_cids
                or oid in journal_oids
            )
            trade_side = str(row.get("side") or "").upper()
            relevant.append((row, bot_owned))
            if not bot_owned:
                unknown.append({"cid": cid, "orderId": oid, "side": trade_side, "qty": str(qty)})
            else:
                increases = (side == "LONG" and trade_side == "BUY") or (side == "SHORT" and trade_side == "SELL")
                bot_net += qty if increases else -qty

        if not relevant or unknown:
            bot.logger.warning(
                "OWNERSHIP PROOF JOURNAL-ENHANCED | %s %s | physical=%s bot_net=%s unknown_or_external=%s proven=False",
                symbol, side, physical_qty, bot_net, unknown[:8],
            )
            return False

        # All relevant exchange trades are attributable to the bot. A quantity mismatch
        # is therefore an internal reconciliation/accounting mismatch, not an external
        # ownership proof. The existing recovery path will close only the physical residual.
        bot.logger.critical(
            "OWNERSHIP PROOF JOURNAL-ENHANCED | %s %s | physical=%s bot_net=%s all_trades_bot_owned=True proven=True",
            symbol, side, physical_qty, bot_net,
        )
        return True
    except Exception as exc:
        bot.logger.warning("OWNERSHIP ENHANCED PROOF FAIL | %s %s | %s", symbol, side, exc)
        return False


bot.Reconciler._prove_bot_owned_residual = _prove_bot_owned_residual_v75


def _repair_state_orphans_before_v74(self):
    try:
        expected = self.expected_by_symbol_side()
        state_mismatches = self.state_ledger_mismatches(expected)
        if not state_mismatches:
            return []
        actual = self.snapshot().positions
        repaired = self._repair_range_state_orphans(state_mismatches, actual)
        if repaired:
            bot.logger.warning(
                "V75 RANGE STATE ORPHAN REPAIR BEFORE OWNERSHIP GUARD | repaired=%s | invoking V74 after repair",
                repaired,
            )
        return repaired
    except Exception as exc:
        bot.logger.exception("V75 RANGE STATE ORPHAN PRE-REPAIR FAILED | fail_closed=True | %s", exc)
        return []


def _reconcile_v75(self, *args, **kwargs):
    try:
        _repair_state_orphans_before_v74(self)
    except Exception as exc:
        bot.logger.exception("V75 PRE-RECONCILE REPAIR FAILED | fail_closed=True | %s", exc)
    return _original_reconcile_v74(self, *args, **kwargs)


bot.Reconciler.reconcile = _reconcile_v75
bot.VERSION = f"{bot.VERSION}-deterministic-orphan-repair-journal-proof-v75"


def main():
    bot.logger.warning(
        "DETERMINISTIC RECONCILIATION V75 ACTIVE | "
        "state_orphan_repair=BEFORE_OWNERSHIP_GUARD | "
        "journal_ownership_proof=ENABLED | positions=UNTOUCHED_UNLESS_PROVEN_BOT_RESIDUAL"
    )
    base.main()


if __name__ == "__main__":
    main()
