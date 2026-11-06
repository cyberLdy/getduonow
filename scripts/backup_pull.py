#!/usr/bin/env python3
"""Pull a DB backup from the deployed site to this Mac (§10 ⑨); run nightly.

Downloads `GET <SITE>/admin/backup` (header X-Backup-Token) to DEST/getduonow-<UTC>.db.gz, keeps it only if it
gunzips, `PRAGMA integrity_check` is ok and table `subscriptions` exists, then keeps the newest 14 backups.
Prints one summary line; exits non-zero on any failure (a bad download never replaces or prunes good ones).

    BACKUP_TOKEN=… [SITE=https://getduonow.onrender.com] [DEST=~/duo-monitor/data/backups] python3 scripts/backup_pull.py
"""
from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import httpx

KEEP = 14
PREFIX, SUFFIX = "getduonow-", ".db.gz"


def verify(gz_path: Path) -> int:
    """Gunzip to a temp file and check it is a sound GetDuoNow DB; returns the number of subscriptions."""
    with tempfile.TemporaryDirectory() as tmp:
        raw = os.path.join(tmp, "check.db")
        with gzip.open(gz_path, "rb") as src, open(raw, "wb") as out:   # a truncated/corrupt gzip raises here
            shutil.copyfileobj(src, out, 1 << 20)
        with closing(sqlite3.connect(raw)) as conn:
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise ValueError(f"integrity_check: {check}")
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='subscriptions'").fetchone():
                raise ValueError("no subscriptions table")
            return conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]


def prune(dest: Path, keep: int = KEEP) -> int:
    """Delete all but the newest `keep` backups (names sort by their UTC timestamp); returns how many remain."""
    files = sorted(dest.glob(f"{PREFIX}*{SUFFIX}"), reverse=True)
    for old in files[keep:]:
        old.unlink()
    return min(len(files), keep)


def pull(site: str, token: str, dest: Path) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    final = dest / f"{PREFIX}{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}{SUFFIX}"
    part = final.with_name(final.name + ".part")
    headers = {"X-Backup-Token": token}
    if os.environ.get("ACCESS_KEY"):   # if the pre-launch gate is ever back on, the share-link cookie gets us through
        headers["Cookie"] = f"gdn_key={os.environ['ACCESS_KEY']}"
    try:
        with httpx.stream("GET", f"{site.rstrip('/')}/admin/backup", headers=headers, timeout=120) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        n_subs = verify(part)
        part.rename(final)
    finally:
        if part.exists():
            part.unlink()
    kept = prune(dest)
    return f"backup ok {final} {final.stat().st_size / 1e6:.2f} MB subscriptions={n_subs} kept={kept}"


def main() -> int:
    token = os.environ.get("BACKUP_TOKEN", "").strip()
    if not token:
        print("backup FAILED: BACKUP_TOKEN not set", file=sys.stderr)
        return 2
    site = os.environ.get("SITE", "https://getduonow.onrender.com")
    dest = Path(os.path.expanduser(os.environ.get("DEST", "~/duo-monitor/data/backups")))
    try:
        print(pull(site, token, dest), flush=True)
    except Exception as e:  # noqa: BLE001 — one line for the launchd log, non-zero exit for the scheduler
        print(f"backup FAILED: {type(e).__name__}: {' '.join(str(e).split())}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
