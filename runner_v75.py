#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Perpetual Principal production entrypoint v75.

V75 fixes a reconciliation deadlock in V74 without touching physical
positions/orders. V69 already contains a narrowly proven repair that restores
missing state legs from the durable ledger when ledger == physical and the
missing durable lots are unambiguously owned by a live RANGE basket.

V74 only reached that repair when inherited reconciliation failed. In the
current incident inherited reconciliation can report success while the
ledger/state projection is still missing a known owned leg. V75 therefore
re-runs the existing V69 proof before V74's ownership guard.

Safety:
- state-only repair uses the existing V69 proof rules;
- no market orders are sent;
- physical positions and open orders are untouched;
- bankroll/equity/RD/risk parameters are untouched by this wrapper;
- unknown physical exposure remains quarantined, never adopted.
"""

import os
import shutil
from pathlib import Path


def _recover_log_storage() -> None:
    bot_dir = Path(os.getenv("BOT_DIR", "/data"))
    log_file = bot_dir / "aster_bot.log"
    try:
        usage = shutil.disk_usage(bot_dir)
        critical_free = 64 * 1024 * 1024
        if usage.free < critical_free:
            try:
                with log_file.open("w", encoding="utf-8"):
                    pass
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


def _reconcile_v75(self, *args, **kwargs):
    try:
        repaired = repair_base._restore_missing_live_range_lots_v69(self)
        if repaired:
            bot.logger.warning(
                "V75 STATE RESTORE RETRY | repairs=%s | positions=UNTOUCHED orders=UNTOUCHED",
                repaired,
            )
    except Exception as exc:
        bot.logger.exception(
            "V75 STATE RESTORE RETRY FAILED | fail_closed=True | %s", exc
        )
    return _original_reconcile_v74(self, *args, **kwargs)


bot.Reconciler.reconcile = _reconcile_v75
bot.VERSION = f"{bot.VERSION}-state-restore-retry-v75"


def main():
    bot.logger.warning(
        "STATE RESTORE RETRY V75 ACTIVE | "
        "repair=durable-ledger-to-state-only | "
        "positions=UNTOUCHED orders=UNTOUCHED"
    )
    base.main()


if __name__ == "__main__":
    main()
