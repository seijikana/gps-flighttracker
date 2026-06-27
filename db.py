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
    country TEXT,
    country_flag TEXT,
    aircraft_type TEXT,
    origin TEXT,
    origin_flag TEXT,
    destination TEXT,
    destination_flag TEXT,
    photo_url TEXT,
    fetched_at REAL NOT NULL
);
"""


def get_conn():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
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
    for column in (
        "country TEXT",
        "country_flag TEXT",
        "origin_flag TEXT",
        "destination_flag TEXT",
        "photo_url TEXT",
    ):
        try:
            conn.execute(f"ALTER TABLE aircraft_info_cache ADD COLUMN {column}")
        except sqlite3.OperationalError:
            pass  # 既に列が存在する場合
    conn.commit()


def start_periodic_checkpoint(interval_sec=60):
    """WALファイルが無制限に肥大化するのを防ぐため、定期的にチェックポイントを実行する。

    GPS/ADS-Bの書き込みが頻発する構成では、チェックポイントを行わないとWALが
    数GB規模まで成長しディスクI/Oエラーの原因になる（実際に発生した事故への対策）。
    専用のコネクションをバックグラウンドスレッドで保持し、他スレッドの書き込みを
    妨げないPASSIVEモードで実行する。
    """
    import threading
    import time

    def _loop():
        conn = sqlite3.connect(config.DB_PATH, timeout=30)
        while True:
            time.sleep(interval_sec)
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.OperationalError:
                pass  # 他スレッドがロック中などは次回に任せる

    threading.Thread(target=_loop, daemon=True).start()


def purge_long_idle_stretches(speed_threshold_kmh, duration_hours, noise_tolerance_sec=30):
    """speed_kmhが連続してspeed_threshold_kmh以下のまま、duration_hours以上続いた区間を
    丸ごと削除する（長時間駐車したまま記録され続けた古いデータの間引き用）。

    信号待ち等の短い停止は区間の継続時間がduration_hours未満のため残る。
    GPS受信機が停車中でも稀に瞬間的な速度ノイズ（マルチパス等）を出すことがあり、単純な
    閾値比較だけだと長時間停車区間がそのノイズで何度も分断され、duration_hours未満の
    断片ばかりになって全く削除されなくなってしまう。そのためnoise_tolerance_sec未満の
    「速い」区間は実質的にノイズとみなし、前後の「遅い」区間と結合してから判定する。
    セッションをまたぐ区間判定はしない（session_idでグループ分けしている）。

    戻り値: 削除した行数
    """
    duration_sec = duration_hours * 3600
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, session_id, ts, speed_kmh FROM session_points ORDER BY session_id, ts"
    )
    rows = cur.fetchall()

    to_delete = []

    def process_session(points):
        """points: [(id, ts, is_slow), ...] を時刻順、1セッション分。"""
        if not points:
            return
        # 1) 生のrun（is_slowが変わるごとに区切る）
        raw_runs = []
        for pid, ts, is_slow in points:
            if raw_runs and raw_runs[-1]["is_slow"] == is_slow:
                raw_runs[-1]["end"] = ts
                raw_runs[-1]["ids"].append(pid)
            else:
                raw_runs.append({"is_slow": is_slow, "start": ts, "end": ts, "ids": [pid]})

        # 2) 短い「速い」runはノイズとみなし「遅い」に再分類
        for r in raw_runs:
            if not r["is_slow"] and (r["end"] - r["start"]) < noise_tolerance_sec:
                r["is_slow"] = True

        # 3) 再分類後、隣接する「遅い」runを結合
        merged = []
        for r in raw_runs:
            if merged and merged[-1]["is_slow"] and r["is_slow"]:
                merged[-1]["end"] = r["end"]
                merged[-1]["ids"].extend(r["ids"])
            else:
                merged.append({"is_slow": r["is_slow"], "start": r["start"], "end": r["end"], "ids": list(r["ids"])})

        # 4) duration_sec以上続く「遅い」runを削除対象に
        for r in merged:
            if r["is_slow"] and (r["end"] - r["start"]) >= duration_sec:
                to_delete.extend(r["ids"])

    current_session_id = None
    buffer = []
    for pid, sid, ts, speed in rows:
        is_slow = (speed if speed is not None else 0.0) <= speed_threshold_kmh
        if sid != current_session_id:
            process_session(buffer)
            buffer = []
            current_session_id = sid
        buffer.append((pid, ts, is_slow))
    process_session(buffer)

    if not to_delete:
        return 0

    with cursor() as c:
        chunk_size = 500
        for i in range(0, len(to_delete), chunk_size):
            chunk = to_delete[i:i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            c.execute(f"DELETE FROM session_points WHERE id IN ({placeholders})", chunk)

    return len(to_delete)


def start_periodic_idle_purge(settings_store, interval_sec=1800):
    """settings_storeのidle_purge_speed_kmh/idle_purge_duration_hrに従い、
    長時間駐車のまま記録された区間を定期的に間引く（起動直後にも1回実行する）。"""
    import threading
    import time

    def _loop():
        while True:
            try:
                s = settings_store.get()
                deleted = purge_long_idle_stretches(
                    s["idle_purge_speed_kmh"], s["idle_purge_duration_hr"]
                )
                if deleted:
                    import logging
                    logging.getLogger(__name__).info(
                        "長時間停車区間を間引きました: %d行削除", deleted
                    )
            except Exception:
                import logging
                logging.getLogger(__name__).exception("idle purge失敗")
            time.sleep(interval_sec)

    threading.Thread(target=_loop, daemon=True).start()
