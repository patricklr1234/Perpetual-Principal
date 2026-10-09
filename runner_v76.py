#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Production storage-recovery bootstrap for V74.

Only removes disposable runtime log files when BOT_DIR is critically full.
Durable trading state, ledgers, journals, orders and positions are never
modified by this bootstrap.
"""
import os
import shutil
from pathlib import Path

_DURABLE_FILES = {
    "state.json", "state.backup.json", "fill_ledger.sqlite3", "trades.jsonl",
    "order_journal.jsonl", "news_calendar_cache.json",
    ".aster_perpetual_bot_dir.instance.lock", "rate_limit_cooldown.json",
}


def _recover_log_storage() -> None:
    bot_dir = Path(os.getenv("BOT_DIR", "/data"))
    try:
        usage = shutil.disk_usage(bot_dir)
        if usage.free >= 64 * 1024 * 1024:
            return
        # Unlink, rather than open(...,'w'): unlink releases the log's blocks
        # even when the filesystem has zero free blocks left.
        for path in bot_dir.glob("aster_bot.log*"):
            if path.name in _DURABLE_FILES:
                continue
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink()
            except Exception:
                pass
    except Exception:
        pass


_recover_log_storage()

import runner_v74 as base  # noqa: E402


def main():
    base.main()


if __name__ == "__main__":
    main()
