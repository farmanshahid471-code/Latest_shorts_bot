"""Process-wide coordination primitives used by the Web UI and scheduler."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

# A single bot package writes to one temp folder, one state DB, and usually one
# encoder. Serializing full pipelines avoids overlapping manual/scheduled jobs,
# while SQLite leases remain the cross-process safety net.
PIPELINE_LOCK = threading.Lock()


@contextmanager
def pipeline_guard(blocking: bool = False) -> Iterator[bool]:
    acquired = PIPELINE_LOCK.acquire(blocking=blocking)
    try:
        yield acquired
    finally:
        if acquired:
            PIPELINE_LOCK.release()
