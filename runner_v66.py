#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Perpetual Principal production entrypoint v66.

Compatibility repair for the v63 ownership-repair fallback: runner_v62 calls
``self.account.snapshot()`` on a Reconciler, while Reconciler's authoritative
exchange snapshot method is ``self.snapshot()`` and it has no ``account``
attribute. Expose a read-only compatibility property returning the reconciler
itself, so the existing fail-closed repair path uses exactly that authoritative
snapshot method without changing positions, orders, state, ledger, bankrolls,
strategy parameters, or risk parameters.
"""

import runner_v64 as base

bot = base.bot

# The inherited v63 repair path expects reconciler.account.snapshot().
# Reconciler.snapshot() is the authoritative implementation in main.py.
if not hasattr(bot.Reconciler, "account"):
    bot.Reconciler.account = property(lambda self: self)

bot.VERSION = f"{bot.VERSION}-reconciler-account-compat-v66"


def main():
    bot.logger.warning(
        "RECONCILER SNAPSHOT COMPAT FIX ACTIVE | version=v66 | "
        "account.snapshot=self.snapshot | mutation=NONE"
    )
    base.main()


if __name__ == "__main__":
    main()
