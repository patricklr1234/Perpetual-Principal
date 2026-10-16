#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Perpetual Principal v77 compatibility repair.

Fixes the proven-residual recovery path's stale ExchangeSnapshot attribute name.
The authoritative snapshot field is entry_prices; V74 referenced entries.
This alias changes no exchange/accounting behavior and only restores the intended
reference-price lookup so an already-proven bot-owned residual can be closed and
reconciled safely.
"""

import main

main.ExchangeSnapshot.entries = property(lambda snapshot: snapshot.entry_prices)

import runner_v75 as base  # noqa: E402

bot = base.bot
bot.VERSION = f"{bot.VERSION}-snapshot-entry-alias-v77"


def main_entry():
    bot.logger.warning(
        "SNAPSHOT ENTRY ALIAS V77 ACTIVE | ExchangeSnapshot.entries -> entry_prices | "
        "proven-residual-recovery=ENABLED | positions=UNTOUCHED_UNLESS_PROVEN"
    )
    base.main()


if __name__ == "__main__":
    main_entry()
