"""In-process sliding-window limits (one Render instance), keyed by visitor IP or by a shared key for global budgets."""
from __future__ import annotations

import threading
import time
from collections import deque


class RateLimit:
    def __init__(self, limit: int, window_s: float):
        self.limit, self.window = limit, window_s
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """Count one request for `key`; False once `limit` requests already happened inside the window."""
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > 20_000:                     # a spread-out flood: forget idle visitors
                self._hits = {k: q for k, q in self._hits.items() if q and now - q[-1] < self.window}
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] >= self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


def client_ip(request) -> str:
    """The visitor's IP. Cloudflare (in front of Render) sets CF-Connecting-IP itself, so it can't be spoofed;
    X-Forwarded-For's first entry is client-supplied and only a fallback."""
    h = request.headers
    ip = h.get("cf-connecting-ip") or h.get("true-client-ip") or (h.get("x-forwarded-for") or "").split(",")[0].strip()
    return ip or (request.client.host if request.client else "unknown")
