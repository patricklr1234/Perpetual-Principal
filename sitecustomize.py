#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Process-wide safety shim for bounded persistent logging.

This module is imported by Python during interpreter startup.  It keeps the
existing production entrypoint intact while making the persistent bot log
bounded and self-recovering when a previous log flood has exhausted /data.
It does not modify state, ledger, bankrolls, orders, positions or BOT_DIR.
"""

import logging
import os

_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 2
_RECOVERY_KEEP_BYTES = 512 * 1024
_LOG_BASENAME = "aster_bot.log"
_OriginalFileHandler = logging.FileHandler


def _recover_persistent_log_space() -> None:
    """Free only obsolete log bytes before the application opens the log.

    This is intentionally limited to the bot's persistent log and its rotated
    copies.  Trading state, SQLite ledger, trade journal and order journal are
    never deleted or truncated here.
    """
    try:
        log_path = os.path.join(os.getenv("BOT_DIR", "/data"), _LOG_BASENAME)
        # Remove only rotated copies created by previous bounded-log versions.
        for idx in range(1, 10):
            rotated = f"{log_path}.{idx}"
            try:
                if os.path.exists(rotated):
                    os.remove(rotated)
            except OSError:
                pass

        if not os.path.exists(log_path):
            return

        size = os.path.getsize(log_path)
        if size <= _RECOVERY_KEEP_BYTES:
            return

        # Read only the tail, truncate first to release space even when the
        # filesystem is completely full, then restore the small tail.
        with open(log_path, "rb") as src:
            src.seek(max(0, size - _RECOVERY_KEEP_BYTES))
            tail = src.read(_RECOVERY_KEEP_BYTES)
        with open(log_path, "wb") as dst:
            dst.write(tail)
    except Exception:
        # Logging/storage recovery must never prevent the trading process from
        # starting. The normal handler below remains fail-safe as well.
        pass


_recover_persistent_log_space()


class _BoundedFileHandler(_OriginalFileHandler):
    """Drop-in FileHandler with deterministic size-based rotation."""

    def __init__(self, filename, mode="a", encoding=None, delay=False, errors=None):
        super().__init__(filename, mode=mode, encoding=encoding, delay=delay, errors=errors)
        self._rotation_announced = False

    def _should_rollover(self, record):
        if self.stream is None:
            self.stream = self._open()
        try:
            msg = self.format(record) + self.terminator
            encoded = msg.encode(self.encoding or "utf-8", errors="replace")
            self.stream.flush()
            current = os.path.getsize(self.baseFilename)
            return current > 0 and current + len(encoded) >= _MAX_BYTES
        except Exception:
            return False

    def _rotate(self):
        if self.stream:
            self.stream.flush()
            self.stream.close()
            self.stream = None

        oldest = f"{self.baseFilename}.{_BACKUP_COUNT}"
        try:
            if os.path.exists(oldest):
                os.remove(oldest)
        except OSError:
            pass

        for idx in range(_BACKUP_COUNT - 1, 0, -1):
            src = f"{self.baseFilename}.{idx}"
            dst = f"{self.baseFilename}.{idx + 1}"
            try:
                if os.path.exists(src):
                    os.replace(src, dst)
            except OSError:
                pass

        try:
            if os.path.exists(self.baseFilename):
                os.replace(self.baseFilename, f"{self.baseFilename}.1")
        finally:
            if not self.delay:
                self.stream = self._open()

    def emit(self, record):
        try:
            if not self._rotation_announced:
                self._rotation_announced = True
                marker = logging.LogRecord(
                    name=record.name,
                    level=logging.WARNING,
                    pathname=__file__,
                    lineno=0,
                    msg=(
                        "PERSISTENT LOG ROTATION ACTIVE | file=%s max_bytes=%s "
                        "backups=%s max_total_approx=%s"
                    ),
                    args=(self.baseFilename, _MAX_BYTES, _BACKUP_COUNT, _MAX_BYTES * (_BACKUP_COUNT + 1)),
                    exc_info=None,
                )
                _OriginalFileHandler.emit(self, marker)

            if self._should_rollover(record):
                self._rotate()
            _OriginalFileHandler.emit(self, record)
        except Exception:
            self.handleError(record)


logging.FileHandler = _BoundedFileHandler
