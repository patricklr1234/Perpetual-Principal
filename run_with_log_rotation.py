#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Production bootstrap that bounds only the persistent text log.

No strategy, risk, state, ledger, bankroll, order, position, BOT_DIR, or Volume
semantics are changed. Railway/stdout logging remains unchanged. The persistent
BOT_DIR/aster_bot.log is rotated at 10 MiB with four backups (~50 MiB maximum).
"""

import logging
from logging.handlers import RotatingFileHandler

import main as bot

MAX_LOG_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 4


def _install_bounded_persistent_log():
    logger = bot.logger
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    # main.py creates one ordinary FileHandler for LOG_FILE at import time.
    # Remove only file handlers; StreamHandler(stdout) is intentionally preserved.
    for handler in list(logger.handlers):
        if isinstance(handler, logging.FileHandler):
            logger.removeHandler(handler)
            try:
                handler.flush()
            finally:
                handler.close()

    rotating = RotatingFileHandler(
        bot.LOG_FILE,
        mode="a",
        maxBytes=MAX_LOG_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
        delay=False,
    )
    rotating.setFormatter(formatter)
    rotating.setLevel(logger.level)
    logger.addHandler(rotating)
    logger.warning(
        "PERSISTENT LOG ROTATION ACTIVE | file=%s max_bytes=%s backups=%s max_total_approx=%s",
        bot.LOG_FILE,
        MAX_LOG_BYTES,
        BACKUP_COUNT,
        MAX_LOG_BYTES * (BACKUP_COUNT + 1),
    )


_install_bounded_persistent_log()

# Import only after logging is bounded. runner imports the same cached main module.
import runner


if __name__ == "__main__":
    runner.main()
