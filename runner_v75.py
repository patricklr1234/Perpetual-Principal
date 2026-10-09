#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Production storage-recovery bootstrap for V74.

Only removes/truncates the bot's disposable runtime log files when the mounted
BOT_DIR is critically full. State, ledger, trade journal, order journal and
other durable trading data are never deleted or modified here.
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
            # The log is disposable; durable trading state is deliberately untouched.
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
        # Storage recovery must never prevent the normal V74 safety bootstrap.
        pass


_recover_log_storage()

import runner_v74 as base  # noqa: E402


# Keep V74 as the trading implementation; this wrapper only makes the
# persistent log self-healing before any logger is constructed.

def main():
    base.main()


if __name__ == "__main__":
    main()
