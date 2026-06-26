"""GPS + IMU を用いた走行セッション判定・記録。

gpsd（DOCTORADIO GR7-10HZ等）が利用できない開発環境では、自動的にモックGPS
（緩やかなランダムウォーク）にフォールバックする。IMUも同様にMPU-6050が
読めない場合はモック値（無振動）にフォールバックする。
"""
import json
import logging
import math
import random
import socket
import threading
import time

import config
import db

logger = logging.getLogger(__name__)


class GpsFix:
    def __init__(self, lat, lon, speed_kmh, heading=None, ts=None):
        self.lat = lat
        self.lon = lon
        self.speed_kmh = speed_kmh
        self.heading = heading
        self.ts = ts if ts is not None else time.time()


class GpsdSource:
    """gpsd (port 2947) のJSONプロトコルから位置・速度を取得する。"""

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self._sock = None
        self._buf = b""

    def _connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=3)
        sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
        self._sock = sock
        self._buf = b""

    def read_fix(self):
        if self._sock is None:
            self._connect()
        while True:
            if b"\n" not in self._buf:
                chunk = self._sock.recv(4096)
                if not chunk:
                    raise ConnectionError("gpsd connection closed")
                self._buf += chunk
                continue
            line, self._buf = self._buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("class") == "TPV" and "lat" in msg and "lon" in msg:
                speed_ms = msg.get("speed", 0.0) or 0.0
                return GpsFix(
                    lat=msg["lat"],
                    lon=msg["lon"],
                    speed_kmh=speed_ms * 3.6,
                    heading=msg.get("track"),
                )

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


class MockGpsSource:
    """gpsd未接続時の開発用モック。緩やかなランダムウォークで自車位置を生成する。"""

    def __init__(self, start_lat=34.6937, start_lon=135.5023):
        self.lat = start_lat
        self.lon = start_lon
        self.heading = 0.0
        self._moving = True
        self._tick = 0

    def read_fix(self):
        time.sleep(1.0)
        self._tick += 1
        # 60秒走行→30秒停車を繰り返す程度のモックパターン
        self._moving = (self._tick % 90) < 60
        if self._moving:
            self.heading = (self.heading + random.uniform(-10, 10)) % 360
            dist_deg = 0.0003
            self.lat += dist_deg * math.cos(math.radians(self.heading))
            self.lon += dist_deg * math.sin(math.radians(self.heading))
            speed_kmh = random.uniform(20, 50)
        else:
            speed_kmh = 0.0
        return GpsFix(lat=self.lat, lon=self.lon, speed_kmh=speed_kmh, heading=self.heading)

    def close(self):
        pass


class ImuSource:
    """MPU-6050から加速度を読み、簡易な振動の有無を判定する。"""

    def __init__(self, bus_num, addr):
        import smbus2  # ローカルimport: ハードウェア未接続環境ではImportErrorでモックに切替

        self.bus = smbus2.SMBus(bus_num)
        self.addr = addr
        self.bus.write_byte_data(self.addr, 0x6B, 0)  # PWR_MGMT_1: wake up

    def _read_word(self, reg):
        high = self.bus.read_byte_data(self.addr, reg)
        low = self.bus.read_byte_data(self.addr, reg + 1)
        val = (high << 8) | low
        if val >= 0x8000:
            val -= 0x10000
        return val

    def read_vibration_g(self):
        ax = self._read_word(0x3B) / 16384.0
        ay = self._read_word(0x3D) / 16384.0
        az = self._read_word(0x3F) / 16384.0
        magnitude = math.sqrt(ax * ax + ay * ay + az * az)
        return abs(magnitude - 1.0)  # 静止時は重力加速度1.0Gのみのはず


class MockImuSource:
    def read_vibration_g(self):
        return 0.0


def _make_gps_source():
    if config.FORCE_MOCK_GPS:
        return MockGpsSource()
    try:
        source = GpsdSource(config.GPSD_HOST, config.GPSD_PORT)
        source._connect()
        logger.info("gpsdに接続しました（%s:%s）", config.GPSD_HOST, config.GPSD_PORT)
        return source
    except OSError as exc:
        logger.warning("gpsdに接続できないためモックGPSを使用します: %s", exc)
        return MockGpsSource()


def _make_imu_source():
    if config.FORCE_MOCK_GPS:
        return MockImuSource()
    try:
        return ImuSource(config.IMU_I2C_BUS, config.IMU_I2C_ADDR)
    except Exception as exc:  # ImportError / OSError 等、ハードウェア未接続を広く許容
        logger.warning("IMUを読み取れないためモックを使用します: %s", exc)
        return MockImuSource()


class SessionManager:
    """走行セッションの開始・継続・終了を判定し、SQLiteに記録する。"""

    def __init__(self):
        self._gps = _make_gps_source()
        self._imu = _make_imu_source()
        self._active_session_id = self._resume_active_session()
        self._last_moving_ts = None
        self._lock = threading.Lock()
        self._thread = None
        self._stop_event = threading.Event()

    @staticmethod
    def _resume_active_session():
        """アプリ再起動時に既存のアクティブセッションを引き継ぐ。

        毎回新規セッションを作ると、駐車モード等で再起動しただけで
        セッションが分かれてしまう。is_active=1の中で最新のものを継続し、
        過去の再起動で残った重複アクティブセッションは終了済みにする。
        """
        with db.cursor() as cur:
            cur.execute(
                "SELECT id FROM sessions WHERE is_active = 1 ORDER BY started_at DESC"
            )
            active_ids = [row["id"] for row in cur.fetchall()]
        if not active_ids:
            return None
        resumed_id = active_ids[0]
        stale_ids = active_ids[1:]
        if stale_ids:
            now = time.time()
            with db.cursor() as cur:
                cur.executemany(
                    "UPDATE sessions SET ended_at = ?, is_active = 0 WHERE id = ?",
                    [(now, sid) for sid in stale_ids],
                )
        return resumed_id

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._gps.close()

    def _run(self):
        while not self._stop_event.is_set():
            try:
                fix = self._gps.read_fix()
            except Exception as exc:
                logger.warning("GPS読み取りエラー、モックに切替します: %s", exc)
                self._gps = MockGpsSource()
                continue
            vibration_g = self._imu.read_vibration_g()
            self._process_fix(fix, vibration_g)

    def _process_fix(self, fix: GpsFix, vibration_g: float):
        now = fix.ts
        is_moving = fix.speed_kmh > config.SESSION_STOP_SPEED_KMH
        has_vibration = vibration_g > config.IMU_VIBRATION_THRESHOLD_G

        with self._lock:
            if is_moving or has_vibration:
                self._last_moving_ts = now

            if self._active_session_id is None:
                self._active_session_id = self._create_session(now)
                self._last_moving_ts = now

            stopped_duration = now - (self._last_moving_ts or now)
            if stopped_duration >= config.SESSION_END_STOP_SECONDS:
                self._end_session(self._active_session_id, now)
                self._active_session_id = self._create_session(now)
                self._last_moving_ts = now

            self._record_point(self._active_session_id, fix)

    @staticmethod
    def _create_session(ts):
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (started_at, ended_at, is_active) VALUES (?, NULL, 1)",
                (ts,),
            )
            return cur.lastrowid

    @staticmethod
    def _end_session(session_id, ts):
        with db.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET ended_at = ?, is_active = 0 WHERE id = ?",
                (ts, session_id),
            )

    @staticmethod
    def _record_point(session_id, fix: GpsFix):
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO session_points (session_id, ts, lat, lon, speed_kmh, heading) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, fix.ts, fix.lat, fix.lon, fix.speed_kmh, fix.heading),
            )

    def current_session_id(self):
        with self._lock:
            return self._active_session_id
