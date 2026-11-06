"""Signed per-subscription links (unsubscribe, extend, "got it?"): /<path>/<sub_id>/<sig>. No login needed, not guessable."""
from __future__ import annotations

import hashlib
import hmac
import os


def _key() -> bytes:
    k = os.environ.get("SECRET_KEY") or os.environ.get("ADMIN_TOKEN") or "dev-only-secret"
    return k.encode()


def sign(sub_id: str, purpose: str) -> str:
    return hmac.new(_key(), f"{purpose}:{sub_id}".encode(), hashlib.sha256).hexdigest()[:20]


def verify(sub_id: str, purpose: str, sig: str) -> bool:
    return hmac.compare_digest(sign(sub_id, purpose), sig or "")


def link(base_url: str, purpose: str, sub_id: str) -> str:
    return f"{base_url.rstrip('/')}/{purpose}/{sub_id}/{sign(sub_id, purpose)}"
