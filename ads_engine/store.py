"""Állapottároló: SQLite (WAL), egyetlen fájl a /data kötetben.

Mit tárol:
  kv         kis beállítások és az utoljára alkalmazott állapot (pl. jóváhagyott keret, projektenként)
  lease      zár: egyszerre egy példány írhat (a Coolify frissítéskor rövid ideig két konténer is élhet)
  runs       minden futás (nap/hét/hónap/ellenőrzés), állapottal és összegzéssel
  actions    MINDEN kifelé ható művelet előtte/utána értékkel (a napló és a visszakövetés alapja)
  jobs       melyik időszak feladata készült el (újraindítás utáni pótlás, nincs dupla futás)
  counters   számlálók (pl. AI-képkeret ISO-hetenként: az OpenAI-hívás ELŐTT nő)
  snapshots  az utolsó ismert élő állapot (ember általi módosítás észleléséhez)
"""
import contextlib
import datetime as dt
import json
import pathlib
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lease (name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, kind TEXT NOT NULL, mode TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL DEFAULT 'running', summary TEXT);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, project TEXT NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL,
  target TEXT, before TEXT, after TEXT, status TEXT NOT NULL, reason TEXT, mode TEXT NOT NULL, request_id TEXT);
CREATE INDEX IF NOT EXISTS actions_run ON actions(run_id);
CREATE INDEX IF NOT EXISTS actions_project_ts ON actions(project, ts);
CREATE TABLE IF NOT EXISTS jobs (
  project TEXT NOT NULL, kind TEXT NOT NULL, period TEXT NOT NULL, status TEXT NOT NULL, run_id INTEGER,
  updated_at TEXT NOT NULL, PRIMARY KEY (project, kind, period));
CREATE TABLE IF NOT EXISTS counters (
  project TEXT NOT NULL, key TEXT NOT NULL, period TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (project, key, period));
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, kind TEXT NOT NULL, ts TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS snapshots_pk ON snapshots(project, kind, id);
CREATE TABLE IF NOT EXISTS llm_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, purpose TEXT NOT NULL, ts TEXT NOT NULL, model TEXT, effort TEXT,
  status TEXT NOT NULL, input_chars INTEGER, input_tokens INTEGER, output_tokens INTEGER, output TEXT, request_id TEXT, error TEXT);
"""


class LeaseBusy(Exception):
    """A zárat egy másik példány tartja."""


def _now_iso(clock=time.time):
    """UTC időbélyeg a tároló óráját követve (a próbák hamis órával dolgoznak, hogy a „ebben a hétben” korlátok ellenőrizhetők legyenek)."""
    return dt.datetime.fromtimestamp(clock(), dt.timezone.utc).isoformat(timespec="seconds")


def _dumps(v):
    return None if v is None else json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)


def _loads(s):
    return None if s is None else json.loads(s)


class Store:
    def __init__(self, path, clock=time.time):
        self.path = str(path)
        if self.path != ":memory:":
            pathlib.Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def close(self):
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ kulcs-érték
    def get(self, key, default=None):
        with self._lock:
            r = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return _loads(r["value"]) if r else default

    def put(self, key, value):
        with self._lock:
            self._db.execute("INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET "
                             "value=excluded.value, updated_at=excluded.updated_at", (key, _dumps(value), _now_iso(self.clock)))

    def delete(self, key):
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE key=?", (key,))

    # ------------------------------------------------------------------ zár
    def acquire(self, name, owner, ttl=900):
        """Zár megszerzése (vagy megújítása, ha már a miénk). A lejárt zárat átvehetjük."""
        now = self.clock()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                r = self._db.execute("SELECT owner, expires_at FROM lease WHERE name=?", (name,)).fetchone()
                if r and r["expires_at"] > now and r["owner"] != owner:
                    self._db.execute("ROLLBACK")
                    return False
                self._db.execute("INSERT INTO lease(name, owner, expires_at) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET "
                                 "owner=excluded.owner, expires_at=excluded.expires_at", (name, owner, now + ttl))
                self._db.execute("COMMIT")
                return True
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def release(self, name, owner):
        with self._lock:
            self._db.execute("DELETE FROM lease WHERE name=? AND owner=?", (name, owner))

    def lease_holder(self, name):
        with self._lock:
            r = self._db.execute("SELECT owner, expires_at FROM lease WHERE name=?", (name,)).fetchone()
        if r and r["expires_at"] > self.clock():
            return r["owner"]
        return None

    @contextlib.contextmanager
    def lease(self, name, owner, ttl=900):
        if not self.acquire(name, owner, ttl):
            raise LeaseBusy(f"a '{name}' zárat egy másik példány tartja ({self.lease_holder(name)})")
        try:
            yield
        finally:
            self.release(name, owner)

    # ------------------------------------------------------------------ futások és napló
    def start_run(self, project, kind, mode):
        with self._lock:
            cur = self._db.execute("INSERT INTO runs(project, kind, mode, started_at) VALUES(?,?,?,?)",
                                   (project, kind, mode, _now_iso(self.clock)))
            return cur.lastrowid

    def finish_run(self, run_id, status, summary=None):
        with self._lock:
            self._db.execute("UPDATE runs SET finished_at=?, status=?, summary=? WHERE id=?",
                             (_now_iso(self.clock), status, _dumps(summary), run_id))

    def last_run(self, project, kind, status=None):
        q, args = "SELECT * FROM runs WHERE project=? AND kind=?", [project, kind]
        if status:
            q += " AND status=?"
            args.append(status)
        with self._lock:
            r = self._db.execute(q + " ORDER BY id DESC LIMIT 1", args).fetchone()
        return self._run(r)

    def runs(self, project=None, limit=20):
        with self._lock:
            if project:
                rows = self._db.execute("SELECT * FROM runs WHERE project=? ORDER BY id DESC LIMIT ?", (project, limit)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._run(r) for r in rows]

    @staticmethod
    def _run(r):
        if not r:
            return None
        d = dict(r)
        d["summary"] = _loads(d.get("summary"))
        return d

    def log_action(self, run_id, project, kind, target, before, after, status, reason="", mode="dry", request_id=""):
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO actions(run_id, project, ts, kind, target, before, after, status, reason, mode, request_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, project, _now_iso(self.clock), kind, target, _dumps(before), _dumps(after), status, reason, mode, request_id))
            return cur.lastrowid

    def actions(self, project=None, run_id=None, kind=None, since=None, limit=200):
        q, args = "SELECT * FROM actions WHERE 1=1", []
        for col, val in (("project", project), ("run_id", run_id), ("kind", kind)):
            if val is not None:
                q += f" AND {col}=?"
                args.append(val)
        if since:
            q += " AND ts>=?"
            args.append(since)
        with self._lock:
            rows = self._db.execute(q + " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["before"], d["after"] = _loads(d["before"]), _loads(d["after"])
            out.append(d)
        return out

    # ------------------------------------------------------------------ feladatok (idempotens ütemezés)
    def job(self, project, kind, period):
        with self._lock:
            r = self._db.execute("SELECT * FROM jobs WHERE project=? AND kind=? AND period=?", (project, kind, period)).fetchone()
        return dict(r) if r else None

    def job_mark(self, project, kind, period, status, run_id=None):
        with self._lock:
            self._db.execute(
                "INSERT INTO jobs(project, kind, period, status, run_id, updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(project, kind, period) DO UPDATE SET status=excluded.status, run_id=excluded.run_id, "
                "updated_at=excluded.updated_at", (project, kind, period, status, run_id, _now_iso(self.clock)))

    # ------------------------------------------------------------------ számlálók
    def counter(self, project, key, period):
        with self._lock:
            r = self._db.execute("SELECT n FROM counters WHERE project=? AND key=? AND period=?", (project, key, period)).fetchone()
        return r["n"] if r else 0

    def counter_add(self, project, key, period, n=1):
        with self._lock:
            self._db.execute(
                "INSERT INTO counters(project, key, period, n) VALUES(?,?,?,?) "
                "ON CONFLICT(project, key, period) DO UPDATE SET n = n + excluded.n", (project, key, period, n))
        return self.counter(project, key, period)

    # ------------------------------------------------------------------ AI-hívások naplója (a nyers kimenet is, ellenőrzéshez)
    def log_llm(self, project, purpose, model, effort, status, input_chars=0, input_tokens=0, output_tokens=0, output="", request_id="", error=""):
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO llm_log(project, purpose, ts, model, effort, status, input_chars, input_tokens, output_tokens, output, request_id, error) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (project, purpose, _now_iso(self.clock), model, effort, status, input_chars, input_tokens, output_tokens, output, request_id, error))
            return cur.lastrowid

    def llm_calls(self, project=None, purpose=None, limit=50):
        q, args = "SELECT * FROM llm_log WHERE 1=1", []
        for col, val in (("project", project), ("purpose", purpose)):
            if val is not None:
                q += f" AND {col}=?"
                args.append(val)
        with self._lock:
            return [dict(r) for r in self._db.execute(q + " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()]

    # ------------------------------------------------------------------ pillanatképek
    def snapshot_put(self, project, kind, data, keep=20):
        with self._lock:
            self._db.execute("INSERT INTO snapshots(project, kind, ts, data) VALUES(?,?,?,?)", (project, kind, _now_iso(self.clock), _dumps(data)))
            self._db.execute("DELETE FROM snapshots WHERE project=? AND kind=? AND id NOT IN "
                             "(SELECT id FROM snapshots WHERE project=? AND kind=? ORDER BY id DESC LIMIT ?)",
                             (project, kind, project, kind, keep))

    def snapshot_get(self, project, kind):
        with self._lock:
            r = self._db.execute("SELECT ts, data FROM snapshots WHERE project=? AND kind=? ORDER BY id DESC LIMIT 1", (project, kind)).fetchone()
        return {"ts": r["ts"], "data": _loads(r["data"])} if r else None
