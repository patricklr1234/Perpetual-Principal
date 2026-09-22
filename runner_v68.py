#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Perpetual Principal production entrypoint v68.

Completes the RANGE ownership repair fix for the deployed ledger schema.
The historical `lots` table in production has no `updated_ms` column, while the
v62 repair helper assumed it existed. v68 keeps the v67 nested-transaction-safe
SAVEPOINT and updates only `open_qty`, the field the repair is intended to
correct. Historical lot rows and all other columns are preserved.
"""

import runner_v67 as base
import runner_v62 as repair

bot = base.bot


def _zero_or_resize_exact_lots_v68(reconciler, updates, reason):
    if not updates:
        return
    ids = list(updates)
    placeholders = ",".join("?" for _ in ids)
    sp = "range_ownership_repair_v68"
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
            for leg_id, new_qty in updates.items():
                nq = bot.dec(new_qty)
                db.execute("UPDATE lots SET open_qty=? WHERE leg_id=?", (str(nq), leg_id))
            db.execute(f"RELEASE SAVEPOINT {sp}")
        except Exception:
            try:
                db.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                db.execute(f"RELEASE SAVEPOINT {sp}")
            except Exception:
                bot.logger.exception("RANGE OWNERSHIP REPAIR V68 | SAVEPOINT ROLLBACK FAILED")
            raise
    bot.logger.warning(
        "RANGE LEDGER OWNERSHIP REPAIR V68 | updates=%s | reason=%s | "
        "tx=SAVEPOINT schema=OPEN_QTY_ONLY",
        updates, reason,
    )


repair._zero_or_resize_exact_lots = _zero_or_resize_exact_lots_v68
bot.VERSION = f"{bot.VERSION}-ledger-schema-v68"


def main():
    bot.logger.warning(
        "RANGE OWNERSHIP LEDGER SCHEMA FIX ACTIVE | version=v68 | tx=SAVEPOINT | "
        "mutation=OPEN_QTY_ONLY | proofs=UNCHANGED | positions=UNTOUCHED "
        "orders=UNTOUCHED risk=UNCHANGED"
    )
    base.main()


if __name__ == "__main__":
    main()
