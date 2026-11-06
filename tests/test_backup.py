import gzip
import importlib
import importlib.util
import re
import sqlite3
import tempfile
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

H = {"X-Backup-Token": "bk-secret"}


def _reload(tmp_path, monkeypatch, token="bk-secret"):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("ADMIN_TOKEN", "adm")
    for k in ("STRIPE_SECRET_KEY", "ACCESS_KEY", "BASIC_AUTH"):
        monkeypatch.delenv(k, raising=False)
    if token:
        monkeypatch.setenv("BACKUP_TOKEN", token)
    else:
        monkeypatch.delenv("BACKUP_TOKEN", raising=False)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))     # snapshot temp files land here, so cleanup is checkable
    import duo_monitor.web as w
    return importlib.reload(w)


@pytest.fixture
def web(tmp_path, monkeypatch):
    return _reload(tmp_path, monkeypatch)


def _sub(w, email):
    return w.db.create_subscription(w.conn, email=email, phone="+15015550123", carrier="unlocked",
                                    watches=[("R300", "Riverside", "MK1E4LL/A", "iPhone Duo 256GB")])


def _open_gz(data: bytes, tmp_path) -> sqlite3.Connection:
    path = tmp_path / f"restored-{len(list(tmp_path.glob('restored-*')))}.db"   # a fresh file per snapshot
    path.write_bytes(gzip.decompress(data))
    return sqlite3.connect(path)


def test_backup_404_when_token_not_configured(tmp_path, monkeypatch):
    c = TestClient(_reload(tmp_path, monkeypatch, token="").app)
    assert c.get("/admin/backup").status_code == 404
    assert c.get("/admin/backup", headers={"X-Backup-Token": ""}).status_code == 404
    assert c.get("/admin/backup", headers={"X-Backup-Token": "anything"}).status_code == 404


def test_backup_401_without_the_backup_token(web):
    c = TestClient(web.app)
    assert c.get("/admin/backup").status_code == 401
    assert c.get("/admin/backup", headers={"X-Backup-Token": "nope"}).status_code == 401
    assert c.get("/admin/backup", headers={"X-Admin-Token": "adm"}).status_code == 401   # admin token is not enough
    assert TestClient(web.app, cookies={"gdn_admin": "adm"}).get("/admin/backup").status_code == 401


def test_backup_returns_gzip_of_a_sound_db_with_the_subscriptions(web, tmp_path):
    ids = {_sub(web, "a@example.com"), _sub(web, "b@example.com")}
    r = TestClient(web.app).get("/admin/backup", headers=H)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/gzip"
    assert re.search(r'filename="getduonow-\d{8}T\d{6}Z\.db\.gz"', r.headers["content-disposition"])
    assert r.headers["cache-control"] == "no-store"
    conn = _open_gz(r.content, tmp_path)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert {row[0] for row in conn.execute("SELECT id FROM subscriptions")} == ids
    assert conn.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 2
    assert list((tmp_path / "tmp").iterdir()) == []                         # temp snapshot removed after sending


def test_backup_works_while_another_connection_is_mid_write_under_wal(web, tmp_path):
    web.conn.execute("PRAGMA journal_mode=WAL")
    done = _sub(web, "done@example.com")
    poller = sqlite3.connect(tmp_path / "t.db")                 # like the poller thread: its own connection
    poller.execute("INSERT INTO subscriptions (id, created_at, status, carrier) VALUES ('half', 'x', 'pending', 'unlocked')")
    c = TestClient(web.app)
    r = c.get("/admin/backup", headers=H)                        # writer still holds its open transaction
    assert r.status_code == 200
    snap = _open_gz(r.content, tmp_path)
    assert snap.execute("PRAGMA journal_mode").fetchone()[0] == "delete"   # WAL source, yet a self-contained file
    assert {row[0] for row in snap.execute("SELECT id FROM subscriptions")} == {done}   # uncommitted row not in it
    poller.commit()
    poller.close()
    ids = {row[0] for row in _open_gz(c.get("/admin/backup", headers=H).content, tmp_path).execute("SELECT id FROM subscriptions")}
    assert ids == {done, "half"}


@pytest.mark.parametrize("journal", ["wal", "delete"])
def test_backup_is_not_stuck_behind_an_open_write_on_the_shared_conn(web, tmp_path, journal):
    """A request thread mid-write on the shared web.conn must neither stall the backup (conn.backup() from web.conn
    gets SQLITE_BUSY there and CPython retries forever) nor leak its uncommitted row into the snapshot."""
    web.conn.execute(f"PRAGMA journal_mode={journal}")
    done = _sub(web, "done@example.com")
    web.conn.execute("INSERT INTO subscriptions (id, created_at, status, carrier) VALUES ('half', 'x', 'pending', 'unlocked')")
    assert web.conn.in_transaction
    out = {}
    t = threading.Thread(target=lambda: out.update(r=TestClient(web.app).get("/admin/backup", headers=H)), daemon=True)
    t.start()
    t.join(5)
    finished = "r" in out
    web.conn.rollback()                                          # unsticks the old behaviour so the suite never hangs
    t.join(5)
    assert finished, "backup blocked behind an uncommitted write on web.conn"
    assert out["r"].status_code == 200
    snap = _open_gz(out["r"].content, tmp_path)
    assert {row[0] for row in snap.execute("SELECT id FROM subscriptions")} == {done}


# ---------- scripts/backup_pull.py ----------

@pytest.fixture
def pull(web, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("backup_pull", Path(__file__).parent.parent / "scripts" / "backup_pull.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    client = TestClient(web.app)
    urls = []
    monkeypatch.setattr(mod.httpx, "stream", lambda method, url, **kw: urls.append(url) or client.stream(method, url, **kw))
    dest = tmp_path / "backups"
    monkeypatch.setenv("SITE", "https://getduonow.test/")
    monkeypatch.setenv("BACKUP_TOKEN", "bk-secret")
    monkeypatch.setenv("DEST", str(dest))
    return mod, dest, urls


def test_pull_downloads_verifies_and_keeps_newest_14(pull, web, capsys):
    mod, dest, urls = pull
    _sub(web, "a@example.com")
    dest.mkdir()
    old = [dest / f"getduonow-200001{d:02d}T000000Z.db.gz" for d in range(1, 17)]   # 16 older backups
    for p in old:
        p.write_bytes(b"x")
    assert mod.main() == 0
    assert urls == ["https://getduonow.test/admin/backup"]
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and out[0].startswith("backup ok ") and "subscriptions=1" in out[0] and "kept=14" in out[0]
    left = sorted(p.name for p in dest.iterdir())
    assert len(left) == 14 and left[:13] == [p.name for p in old[3:]]       # the 3 oldest went, the new one stayed
    assert mod.verify(dest / left[-1]) == 1


def test_pull_fails_loudly_and_keeps_old_backups_on_bad_token(pull, web, monkeypatch, capsys):
    mod, dest, _ = pull
    client = TestClient(web.app)    # server and script share os.environ here, so swap the header the script sends
    monkeypatch.setattr(mod.httpx, "stream", lambda method, url, headers, **kw:
                        client.stream(method, url, headers={**headers, "X-Backup-Token": "wrong"}, **kw))
    dest.mkdir()
    old = [dest / f"getduonow-200001{d:02d}T000000Z.db.gz" for d in range(1, 17)]
    for p in old:
        p.write_bytes(b"x")
    assert mod.main() == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and err[0].startswith("backup FAILED") and "401" in err[0]
    assert sorted(dest.iterdir()) == old                                     # nothing added, nothing pruned


def test_pull_refuses_without_token(pull, monkeypatch):
    mod, _, urls = pull
    monkeypatch.delenv("BACKUP_TOKEN")
    assert mod.main() != 0 and urls == []


def test_verify_rejects_truncated_gzip_and_foreign_db(pull, tmp_path):
    mod = pull[0]
    good = tmp_path / "good.db"
    with sqlite3.connect(good) as c:
        c.execute("CREATE TABLE subscriptions (id TEXT)")
    data = gzip.compress(good.read_bytes())
    (tmp_path / "trunc.db.gz").write_bytes(data[:len(data) // 2])
    with pytest.raises(Exception):
        mod.verify(tmp_path / "trunc.db.gz")
    other = tmp_path / "other.db"
    with sqlite3.connect(other) as c:
        c.execute("CREATE TABLE t (x)")
    (tmp_path / "other.db.gz").write_bytes(gzip.compress(other.read_bytes()))
    with pytest.raises(ValueError, match="subscriptions"):
        mod.verify(tmp_path / "other.db.gz")
    (tmp_path / "good.db.gz").write_bytes(data)
    assert mod.verify(tmp_path / "good.db.gz") == 0
