#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Process-wide safety shim for bounded FileHandler logs.

Python imports ``sitecustomize`` automatically during interpreter startup when
this repository is on sys.path (the normal ``python runner.py`` Railway launch).
This keeps the existing production entrypoint unchanged while making ordinary
``logging.FileHandler`` instances bounded. It does not touch stdout handlers,
strategy/risk parameters, state, ledger, bankrolls, orders, positions, BOT_DIR,
or the Railway Volume.
"""

import logging
import os

_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 4
_OriginalFileHandler = logging.FileHandler


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
            # Logging must never take the trading process down. If the size check
            # itself is unavailable, fall back to the original FileHandler emit.
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


# main.py currently constructs the persistent /data/aster_bot.log with
# logging.FileHandler. Replacing that class here means the existing runner.py
# needs no entrypoint change and the production redeploy automatically picks up
# bounded rotation.
logging.FileHandler = _BoundedFileHandler
