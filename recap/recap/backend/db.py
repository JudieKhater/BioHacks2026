"""
Storage for Recap.

Two interchangeable backends with the same methods:
  - PostgresStore: Tiger Data (TimescaleDB). Used when DATABASE_URL is set.
    Tables are hypertables; trends use time_bucket(). See schema_timescale.sql.
  - SQLiteStore:   zero-setup local file, so the app runs on any laptop.

Privacy: no audio and no live caption text is ever stored. Only numbers
(features), breathing-trainer results, consent events, session reports
(numbers and generated guidance), and recap text that Alex has explicitly
approved for his students.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

FEATURE_COLS = ["wpm", "loudness_db", "breath_group_words", "effort", "speaking_s",
                "confidence", "score", "captions_on"]


def _day_part(ts: float) -> str:
    return "morning" if datetime.fromtimestamp(ts).hour < 12 else "afternoon"


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def summarize_rows(rows: list[dict], emst_rows: list[dict]) -> list[dict]:
    """
    Build per-day trend rows: morning vs afternoon averages + breathing trainer.
    Shared by both stores so the clinic dashboard looks identical.
    """
    by_day: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        day = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")
        part = _day_part(r["ts"])
        for k in ("wpm", "loudness_db", "breath_group_words", "score"):
            by_day[day][f"{part}_{k}"].append(r.get(k))
        by_day[day]["speaking_s"].append(r.get("speaking_s") or 0)
        by_day[day]["support"].append(1 if (r.get("wpm") or 999) < 125 else 0)

    emst_by_day: dict[str, list] = defaultdict(list)
    for e in emst_rows:
        day = datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d")
        emst_by_day[day].append(e)

    out = []
    for day in sorted(set(by_day) | set(emst_by_day)):
        d = by_day.get(day, {})
        row = {"day": day}
        for part in ("morning", "afternoon"):
            for k in ("wpm", "loudness_db", "breath_group_words", "score"):
                row[f"{part}_{k}"] = _mean(d.get(f"{part}_{k}", []))
        row["speaking_min"] = round(sum(d.get("speaking_s", [])) / 60.0, 1)
        sup = d.get("support", [])
        row["support_pct"] = round(100 * sum(sup) / len(sup)) if sup else None
        es = emst_by_day.get(day, [])
        row["emst_reps"] = sum(e.get("reps") or 0 for e in es)
        row["emst_peak_cmh2o"] = max((e.get("peak_cmh2o") or 0 for e in es), default=None) if es else None
        out.append(row)
    return out


class SQLiteStore:
    kind = "sqlite"

    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS speech_features (
                ts REAL NOT NULL, wpm REAL, loudness_db REAL, breath_group_words REAL,
                effort REAL, speaking_s REAL, confidence REAL, score REAL, captions_on INTEGER);
            CREATE INDEX IF NOT EXISTS idx_sf_ts ON speech_features(ts);
            CREATE TABLE IF NOT EXISTS emst_sessions (
                ts REAL NOT NULL, reps INTEGER, peak_cmh2o REAL, target_cmh2o REAL, notes TEXT);
            CREATE TABLE IF NOT EXISTS recaps (
                ts REAL NOT NULL, day TEXT NOT NULL, sentences TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS consent_events (
                ts REAL NOT NULL, party TEXT, granted INTEGER, receipt_hash TEXT, chain_tx TEXT);
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, started REAL NOT NULL, ended REAL NOT NULL, report TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started);
            """)
            self.conn.commit()

    def _exec(self, sql, params=()):
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def insert_features(self, ts: float, f: dict) -> None:
        vals = [f.get(c) for c in FEATURE_COLS]
        self._exec(f"INSERT INTO speech_features (ts, {', '.join(FEATURE_COLS)}) VALUES (?{', ?' * len(FEATURE_COLS)})",
                   [ts, *vals])

    def insert_emst(self, ts, reps, peak, target, notes=None):
        self._exec("INSERT INTO emst_sessions VALUES (?,?,?,?,?)", (ts, reps, peak, target, notes))

    def save_recap(self, ts, day, sentences):
        self._exec("INSERT INTO recaps VALUES (?,?,?)", (ts, day, json.dumps(sentences)))

    def latest_recap(self) -> Optional[dict]:
        r = self._exec("SELECT * FROM recaps ORDER BY ts DESC LIMIT 1").fetchone()
        return {"ts": r["ts"], "day": r["day"], "sentences": json.loads(r["sentences"])} if r else None

    def insert_consent(self, ts, party, granted, receipt_hash, chain_tx):
        self._exec("INSERT INTO consent_events VALUES (?,?,?,?,?)", (ts, party, int(granted), receipt_hash, chain_tx))

    def consent_log(self, limit=20):
        rows = self._exec("SELECT * FROM consent_events ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def trends(self, days: int = 14) -> list[dict]:
        since = time.time() - days * 86400
        rows = [dict(r) for r in self._exec("SELECT * FROM speech_features WHERE ts >= ?", (since,)).fetchall()]
        emst = [dict(r) for r in self._exec("SELECT * FROM emst_sessions WHERE ts >= ?", (since,)).fetchall()]
        return summarize_rows(rows, emst)

    # Session reports: numbers and generated guidance only, never transcript text.
    def insert_session(self, report: dict) -> None:
        self._exec("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?)",
                   (report["id"], report["started"], report["ended"], json.dumps(report)))

    def list_sessions(self, limit: int = 30) -> list[dict]:
        """Full reports, newest first."""
        rows = self._exec("SELECT report FROM sessions ORDER BY started DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r["report"]) for r in rows]

    def get_session(self, sid: str) -> Optional[dict]:
        r = self._exec("SELECT report FROM sessions WHERE id = ?", (sid,)).fetchone()
        return json.loads(r["report"]) if r else None

    def today_curve(self) -> list[dict]:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        rows = self._exec("SELECT ts, wpm, loudness_db, score, captions_on FROM speech_features WHERE ts >= ? ORDER BY ts",
                          (start,)).fetchall()
        return [dict(r) for r in rows]


class PostgresStore:
    """Tiger Data / TimescaleDB. Requires `pip install "psycopg[binary]"`."""
    kind = "timescale"

    def __init__(self, url: str):
        import psycopg  # imported lazily so SQLite users don't need it
        from psycopg.rows import dict_row
        self.conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        self.lock = threading.Lock()
        schema = (Path(__file__).parent / "schema_timescale.sql").read_text()
        # Run statements one at a time: continuous aggregates can't be created
        # inside the implicit transaction a multi-statement string would open.
        lines = [ln for ln in schema.splitlines() if not ln.strip().startswith("--")]
        statements = [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
        with self.lock:
            for stmt in statements:
                self.conn.execute(stmt)

    def _exec(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params)

    def insert_features(self, ts, f):
        vals = [f.get(c) for c in FEATURE_COLS]
        vals[-1] = bool(vals[-1]) if vals[-1] is not None else None
        self._exec(f"INSERT INTO speech_features (time, {', '.join(FEATURE_COLS)}) "
                   f"VALUES (to_timestamp(%s){', %s' * len(FEATURE_COLS)})", [ts, *vals])

    def insert_emst(self, ts, reps, peak, target, notes=None):
        self._exec("INSERT INTO emst_sessions (time, reps, peak_cmh2o, target_cmh2o, notes) "
                   "VALUES (to_timestamp(%s), %s, %s, %s, %s)", (ts, reps, peak, target, notes))

    def save_recap(self, ts, day, sentences):
        self._exec("INSERT INTO recaps (time, day, sentences) VALUES (to_timestamp(%s), %s, %s)",
                   (ts, day, json.dumps(sentences)))

    def latest_recap(self):
        r = self._exec("SELECT extract(epoch FROM time) AS ts, day, sentences FROM recaps ORDER BY time DESC LIMIT 1").fetchone()
        if not r:
            return None
        s = r["sentences"]
        return {"ts": float(r["ts"]), "day": r["day"], "sentences": json.loads(s) if isinstance(s, str) else s}

    def insert_consent(self, ts, party, granted, receipt_hash, chain_tx):
        self._exec("INSERT INTO consent_events (time, party, granted, receipt_hash, chain_tx) "
                   "VALUES (to_timestamp(%s), %s, %s, %s, %s)", (ts, party, granted, receipt_hash, chain_tx))

    def consent_log(self, limit=20):
        rows = self._exec("SELECT extract(epoch FROM time) AS ts, party, granted, receipt_hash, chain_tx "
                          "FROM consent_events ORDER BY time DESC LIMIT %s", (limit,)).fetchall()
        return [{**r, "ts": float(r["ts"])} for r in rows]

    def trends(self, days: int = 14):
        # time_bucket() is TimescaleDB: 15-minute buckets keep the payload small
        # even after months of 5-second feature windows.
        rows = self._exec("""
            SELECT extract(epoch FROM time_bucket('15 minutes', time)) AS ts,
                   avg(wpm) AS wpm, avg(loudness_db) AS loudness_db,
                   avg(breath_group_words) AS breath_group_words, avg(score) AS score,
                   sum(speaking_s) AS speaking_s
            FROM speech_features
            WHERE time >= now() - make_interval(days => %s)
            GROUP BY 1 ORDER BY 1""", (days,)).fetchall()
        emst = self._exec("""
            SELECT extract(epoch FROM time) AS ts, reps, peak_cmh2o
            FROM emst_sessions WHERE time >= now() - make_interval(days => %s)""", (days,)).fetchall()
        norm = lambda rs: [{k: (float(v) if v is not None and k != "reps" else v) for k, v in r.items()} for r in rs]
        return summarize_rows(norm(rows), norm(emst))

    def insert_session(self, report: dict) -> None:
        self._exec("INSERT INTO sessions (id, started, ended, report) "
                   "VALUES (%s, to_timestamp(%s), to_timestamp(%s), %s) "
                   "ON CONFLICT (id) DO UPDATE SET report = EXCLUDED.report, ended = EXCLUDED.ended",
                   (report["id"], report["started"], report["ended"], json.dumps(report)))

    def list_sessions(self, limit: int = 30) -> list[dict]:
        rows = self._exec("SELECT report FROM sessions ORDER BY started DESC LIMIT %s", (limit,)).fetchall()
        return [r["report"] if isinstance(r["report"], dict) else json.loads(r["report"]) for r in rows]

    def get_session(self, sid: str) -> Optional[dict]:
        r = self._exec("SELECT report FROM sessions WHERE id = %s", (sid,)).fetchone()
        if not r:
            return None
        return r["report"] if isinstance(r["report"], dict) else json.loads(r["report"])

    def today_curve(self):
        rows = self._exec("""
            SELECT extract(epoch FROM time) AS ts, wpm, loudness_db, score, captions_on
            FROM speech_features WHERE time >= date_trunc('day', now()) ORDER BY time""").fetchall()
        return [{**r, "ts": float(r["ts"])} for r in rows]


def make_store(database_url: str, sqlite_path: str):
    if database_url:
        return PostgresStore(database_url)
    return SQLiteStore(sqlite_path)
