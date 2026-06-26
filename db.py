import sqlite3
import threading
from contextlib import contextmanager

import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    ended_at REAL,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS session_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    ts REAL NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    speed_kmh REAL,
    heading REAL
);
CREATE INDEX IF NOT EXISTS idx_session_points_session ON session_points(session_id);

CREATE TABLE IF NOT EXISTS aircraft_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    icao TEXT NOT NULL,
    session_id INTEGER REFERENCES sessions(id),
    ts REAL NOT NULL,
    lat REAL,
    lon REAL,
    altitude_ft REAL,
    speed_kt REAL,
    track_deg REAL,
    callsign TEXT
);
CREATE INDEX IF NOT EXISTS idx_aircraft_tracks_icao ON aircraft_tracks(icao, ts);

CREATE TABLE IF NOT EXISTS aircraft_info_cache (
    icao TEXT PRIMARY KEY,
    callsign TEXT,
    airline TEXT,
    aircraft_type TEXT,
    origin TEXT,
    destination TEXT,
    fetched_at REAL NOT NULL
);
"""


def get_conn():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        _local.conn = conn
    return conn


@contextmanager
def cursor():
    conn = get_conn()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
