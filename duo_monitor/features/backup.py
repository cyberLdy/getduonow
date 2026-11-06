"""Nightly DB backup (§10 ⑨): GET /admin/backup streams a gzip of a consistent SQLite snapshot.

Auth is its own token, not the site gate and not ADMIN_TOKEN: header X-Backup-Token must equal env BACKUP_TOKEN.
No BACKUP_TOKEN → 404, wrong token → 401. The owner's Mac pulls it with scripts/backup_pull.py.
The snapshot uses SQLite's online backup API in one step, so it is a single read transaction: it never sees a
half-written poller round, and under WAL the poller keeps writing while it runs. It reads web.conn's DB file through a
connection of its own: backing up from the shared web.conn would copy under its mutex (stalling every request) and,
while any request thread is mid-write on it, get SQLITE_BUSY, which CPython's backup() retries forever.
"""
from __future__ import annotations

import gzip
import os
import secrets
import shutil
import sqlite3
import tempfile
from contextlib import closing

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from duo_monitor import db

router = APIRouter()


def snapshot(conn: sqlite3.Connection, gz_path: str) -> None:
    """Copy `conn`'s database into a self-contained (non-WAL) SQLite file and gzip it to `gz_path`."""
    raw = gz_path + ".db"
    try:
        with closing(sqlite3.connect(raw)) as dst:
            conn.backup(dst)                               # pages=-1: one step = one consistent snapshot
            dst.execute("PRAGMA journal_mode=DELETE")      # the copy inherits WAL; make it a single file
        with open(raw, "rb") as src, gzip.open(gz_path, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
    finally:
        if os.path.exists(raw):
            os.unlink(raw)


@router.get("/admin/backup")
def backup(request: Request) -> FileResponse:
    token = os.environ.get("BACKUP_TOKEN", "").strip()
    if not token:
        raise HTTPException(404)
    if not secrets.compare_digest(request.headers.get("x-backup-token", "").encode(), token.encode()):
        raise HTTPException(401, "backup token required")
    from duo_monitor import web
    tmp = tempfile.mkdtemp(prefix="gdn-backup-")
    name = f"getduonow-{db.now().strftime('%Y%m%dT%H%M%SZ')}.db.gz"
    try:
        path = web.conn.execute("PRAGMA database_list").fetchone()[2]   # main DB file behind web.conn
        with closing(sqlite3.connect(path)) as src:                     # own connection: sees committed data only
            snapshot(src, os.path.join(tmp, name))
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return FileResponse(os.path.join(tmp, name), media_type="application/gzip", filename=name,
                        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
                        background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True))
