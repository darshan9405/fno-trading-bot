"""Tiny thread-safe TTL cache used to dedupe repeated broker calls.

Design notes
------------
* All caches are process-local. The backend runs a single gunicorn worker
  (see Dockerfile.backend) plus APScheduler threads that already share state,
  so a module-level singleton is sufficient and matches "single worker + many
  threads" from deploy/entrypoint.sh.
* Refresh (``callable()``) is invoked OUTSIDE the cache lock so a slow upstream
  cannot serialise concurrent readers. The lock only protects the cache slot
  itself during the swap.
* Invalidations are explicit: callers that mutate upstream state should call
  :py:meth:`TTLCache.invalidate` (single key) or :py:meth:`TTLCache.clear` (all).
  Write wrappers in :mod:`app.broker.upstox_broker` do this automatically after
  place/modify/cancel so caches never read post-mutation for longer than the
  configured TTL.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable


class TTLCache:
    """A minimal TTL cache keyed on string, value of any type.

    ``ttl_s <= 0`` means "always refresh" (i.e. effectively disabled, useful for
    tests and runtime overrides via env).
    """

    __slots__ = ("_ttl", "_data", "_lock", "_hits", "_misses")

    def __init__(self, ttl_s: float) -> None:
        self._ttl = float(ttl_s)
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    # ----- introspection ---------------------------------------------------

    @property
    def ttl(self) -> float:
        return self._ttl

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self._hits, "misses": self._misses, "size": len(self._data)}

    # ----- core ops --------------------------------------------------------

    def get(self, key: str, refresh: Callable[[], Any]) -> Any:
        """Return the cached value if fresh; otherwise call ``refresh``."""
        if self._ttl <= 0:
            # Caching disabled — call every time. Counted as a "miss".
            with self._lock:
                self._misses += 1
            return refresh()

        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit is not None and (now - hit[0]) < self._ttl:
                self._hits += 1
                return hit[1]
            self._misses += 1

        value = refresh()
        with self._lock:
            self._data[key] = (time.monotonic(), value)
        return value

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def set_ttl(self, ttl_s: float) -> None:
        self._ttl = float(ttl_s)


__all__ = ["TTLCache"]
