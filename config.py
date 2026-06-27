import os

DB_PATH = os.environ.get("TRACKER_DB_PATH", os.path.join(os.path.dirname(__file__), "tracker.db"))

# GPS / gpsd
GPSD_HOST = os.environ.get("GPSD_HOST", "localhost")
GPSD_PORT = int(os.environ.get("GPSD_PORT", "2947"))

# IMU (GY-521 / MPU-6050)
IMU_I2C_BUS = int(os.environ.get("IMU_I2C_BUS", "1"))
IMU_I2C_ADDR = int(os.environ.get("IMU_I2C_ADDR", "0x68"), 0)

# 走行セッション判定の閾値
SESSION_STOP_SPEED_KMH = float(os.environ.get("SESSION_STOP_SPEED_KMH", "1.0"))
SESSION_END_STOP_SECONDS = int(os.environ.get("SESSION_END_STOP_SECONDS", str(60 * 60)))  # 1時間
IMU_VIBRATION_THRESHOLD_G = float(os.environ.get("IMU_VIBRATION_THRESHOLD_G", "0.05"))
# GPSは10Hzで取得できるが、軌跡記録としては1秒間隔で十分なためDB書き込みを間引く
# （無制限の書き込みはWALファイル肥大化・ディスクI/Oエラーの原因になるため）
SESSION_POINT_MIN_INTERVAL_SEC = float(os.environ.get("SESSION_POINT_MIN_INTERVAL_SEC", "1.0"))

# ADS-B (dump1090 / readsb)
DUMP1090_URL = os.environ.get("DUMP1090_URL", "http://localhost:8080/data/aircraft.json")
ADSB_POLL_INTERVAL_SEC = float(os.environ.get("ADSB_POLL_INTERVAL_SEC", "2.0"))
ADSB_AIRCRAFT_TIMEOUT_SEC = float(os.environ.get("ADSB_AIRCRAFT_TIMEOUT_SEC", str(3 * 60)))

# 機体情報補完
AIRCRAFT_DB_CSV_PATH = os.environ.get("AIRCRAFT_DB_CSV_PATH", "")  # OpenSky aircraftDatabase.csv
OPENSKY_API_BASE = os.environ.get("OPENSKY_API_BASE", "https://opensky-network.org/api")
ENRICH_TIMEOUT_SEC = float(os.environ.get("ENRICH_TIMEOUT_SEC", "3.0"))
ENRICH_CACHE_TTL_SEC = int(os.environ.get("ENRICH_CACHE_TTL_SEC", str(24 * 60 * 60)))

# 通知音
NOTIFY_SOUND_PATH = os.environ.get(
    "NOTIFY_SOUND_PATH", os.path.join(os.path.dirname(__file__), "static", "sounds", "new_aircraft.wav")
)
NOTIFY_SOUND_ENABLED = os.environ.get("NOTIFY_SOUND_ENABLED", "1") != "0"

# 地図タイル（オフライン、USB HDD等にダウンロードした日本全国タイル）
TILE_PATH = os.environ.get("TILE_PATH", os.path.join(os.path.dirname(__file__), "tiles"))

# Webサーバー
HOST = os.environ.get("TRACKER_HOST", "0.0.0.0")
PORT = int(os.environ.get("TRACKER_PORT", "5001"))
DEBUG = os.environ.get("TRACKER_DEBUG", "0") == "1"

# モックモード（ハードウェア未接続時に自動フォールバックするが、明示的に強制することも可能）
FORCE_MOCK_GPS = os.environ.get("FORCE_MOCK_GPS", "0") == "1"
FORCE_MOCK_ADSB = os.environ.get("FORCE_MOCK_ADSB", "0") == "1"
