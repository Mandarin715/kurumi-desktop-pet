"""Opt-in FX diagnostics. Callers provide only safe scalar fields, never payloads."""
from datetime import datetime
import json
import logging
import time

LOGGER = logging.getLogger('kurumi.fx.debug')
# A dedicated child logger lets opt-in records reach the existing file handler
# while the application's normal log level remains WARNING.
LOGGER.setLevel(logging.INFO)


def stamp():
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


def record(enabled, event, **fields):
    if enabled:
        LOGGER.info('FX_DEBUG %s', json.dumps(
            {'time': stamp(), 'monotonic_s': round(time.monotonic(), 6),
             'event': event, **fields}, ensure_ascii=False, allow_nan=False))
