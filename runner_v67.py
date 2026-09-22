#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Perpetual Principal production entrypoint v67.

Fixes the v63 RANGE ownership repair when SQLite already has an active
transaction. The inherited helper used BEGIN IMMEDIATE unconditionally and
could raise "cannot start a transaction within a transaction", leaving a
proven stale RANGE ledger owner unrepaired and the bot correctly fail-closed.

This patch changes only the repair transaction primitive to a SAVEPOINT, which
is valid both inside and outside an existing SQLite transaction. All original
proof conditions remain in runner_v62: exactly one live RANGE state owner,
physical qty <= state qty, stale ledger owner not represented by state, no live
orders owned by the stale strategy, and exact lot-set revalidation before the
mutation. Positions/orders/risk parameters are untouched.
"""

import runner_v66 as base
import runner_v62 as repair

bot = base.bot


def _zero_or_resize_exact_lots_v67(reconciler, updates, reason):
    if not updates:
        return
    ids = list(updates)
    placeholders = ",".join("?" for _ in ids)
    sp = "range_ownership_repair_v67"
    with reconciler.ledger.lock:
        db = reconciler.ledger.db
        db.execute(f"SAVEPOINT {sp}")
        try:
            rows = db.execute(
                f"SELECT leg_id, open_qty FROM lots WHERE leg_id IN ({placeholders})", ids
            ).fetchall()
            current = {str(i): bot.dec(q) for i, q in rows}
            if set(current) != set(ids):
                raise RuntimeError(f"ledger lot set changed/missing before commit: {current}")
            t = bot.now_ms()
            for leg_id, new_qty in updates.items():
                nq = bot.dec(new_qty)
                db.execute(
                    "UPDATE lots SET open_qty=?, updated_ms=? WHERE leg_id=?",
                    (str(nq), t, leg_id),
                )
            db.execute(f"RELEASE SAVEPOINT {sp}")
        except Exception:
            try:
                db.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                db.execute(f"RELEASE SAVEPOINT {sp}")
            except Exception:
                bot.logger.exception("RANGE OWNERSHIP REPAIR V67 | SAVEPOINT ROLLBACK FAILED")
            raise
    bot.logger.warning(
        "RANGE LEDGER OWNERSHIP REPAIR V67 | updates=%s | reason=%s | tx=SAVEPOINT",
        updates, reason,
    )


# _repair_unambiguous_stale_range_ownership resolves this helper from the
# runner_v62 module globals at call time, so replace only that transaction
# helper and retain all inherited safety proofs and reconciliation behavior.
repair._zero_or_resize_exact_lots = _zero_or_resize_exact_lots_v67
bot.VERSION = f"{bot.VERSION}-range-repair-savepoint-v67"


def main():
    bot.logger.warning(
        "RANGE OWNERSHIP SQLITE FIX ACTIVE | version=v67 | tx=SAVEPOINT | "
        "proofs=UNCHANGED | positions=UNTOUCHED orders=UNTOUCHED risk=UNCHANGED"
    )
    base.main()


if __name__ == "__main__":
    main()
