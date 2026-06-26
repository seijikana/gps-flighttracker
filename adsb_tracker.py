"""dump1090/readsbのaircraft.jsonをポーリングし、現在検出中の機体のみを管理する。

ADS-B受信機（RTL-SDR + dump1090）が無い開発環境では、自動的にモック機体生成に
フォールバックする。新規機体を検出した時点で notify_sound 経由で通知音を鳴らし、
aircraft_enrich で機種・エアライン・出発地/目的地を補完する。
"""
import logging
import random
import threading
import time

import requests

import aircraft_enrich
import config
import db
import notify_sound

logger = logging.getLogger(__name__)


class AdsbTracker:
    def __init__(self, session_id_provider=None):
        """session_id_provider: 現在の走行セッションIDを返す callable（無ければNone）。"""
        self._session_id_provider = session_id_provider or (lambda: None)
        self._aircraft = {}  # icao -> dict(最新情報)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._use_mock = config.FORCE_MOCK_ADSB
        if not self._use_mock:
            try:
                requests.get(config.DUMP1090_URL, timeout=2)
            except requests.RequestException as exc:
                logger.warning("dump1090に接続できないためモックADS-Bを使用します: %s", exc)
                self._use_mock = True

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def _run(self):
        while not self._stop_event.is_set():
            try:
                aircraft_list = self._fetch_mock() if self._use_mock else self._fetch_dump1090()
                self._update(aircraft_list)
            except Exception as exc:
                logger.warning("ADS-Bポーリングエラー: %s", exc)
            time.sleep(config.ADSB_POLL_INTERVAL_SEC)

    def _fetch_dump1090(self):
        resp = requests.get(config.DUMP1090_URL, timeout=config.ADSB_POLL_INTERVAL_SEC)
        resp.raise_for_status()
        data = resp.json()
        return data.get("aircraft", [])

    _mock_state = None

    def _fetch_mock(self):
        if self._mock_state is None:
            self._mock_state = [
                {"hex": "abc123", "flight": "ANA123", "lat": 34.70, "lon": 135.50, "alt": 32000, "spd": 420, "track": 90},
                {"hex": "def456", "flight": "JAL456", "lat": 34.65, "lon": 135.55, "alt": 28000, "spd": 380, "track": 270},
            ]
        for ac in self._mock_state:
            ac["lat"] += random.uniform(-0.01, 0.01)
            ac["lon"] += random.uniform(-0.01, 0.01)
        # 一定確率で新規機体が出現/既存機体がロストするのを模した挙動
        if random.random() < 0.02:
            new_hex = f"mock{random.randint(1000, 9999)}"
            self._mock_state.append(
                {
                    "hex": new_hex,
                    "flight": f"MOCK{random.randint(1, 999)}",
                    "lat": 34.68 + random.uniform(-0.05, 0.05),
                    "lon": 135.52 + random.uniform(-0.05, 0.05),
                    "alt": random.choice([15000, 25000, 35000]),
                    "spd": random.uniform(250, 450),
                    "track": random.uniform(0, 360),
                }
            )
        return list(self._mock_state)

    def _update(self, aircraft_list):
        now = time.time()
        seen_icaos = set()
        new_icaos = []

        with self._lock:
            for ac in aircraft_list:
                icao = ac.get("hex")
                if not icao:
                    continue
                seen_icaos.add(icao)
                if icao not in self._aircraft:
                    new_icaos.append(icao)
                self._aircraft[icao] = {
                    "icao": icao,
                    "callsign": (ac.get("flight") or "").strip(),
                    "lat": ac.get("lat"),
                    "lon": ac.get("lon"),
                    "altitude_ft": ac.get("alt"),
                    "speed_kt": ac.get("spd"),
                    "track_deg": ac.get("track"),
                    "last_seen": now,
                    "info": None,
                }

            # ロストした機体（タイムアウト超過）は「現在検出中」一覧から除外する
            stale = [
                icao
                for icao, data_ in self._aircraft.items()
                if now - data_["last_seen"] > config.ADSB_AIRCRAFT_TIMEOUT_SEC
            ]
            for icao in stale:
                del self._aircraft[icao]

            session_id = self._session_id_provider()
            for icao, data_ in self._aircraft.items():
                if icao in seen_icaos and data_["lat"] is not None and data_["lon"] is not None:
                    self._record_track_point(icao, session_id, now, data_)

        for icao in new_icaos:
            self._on_new_aircraft(icao)

    @staticmethod
    def _record_track_point(icao, session_id, ts, data_):
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO aircraft_tracks "
                "(icao, session_id, ts, lat, lon, altitude_ft, speed_kt, track_deg, callsign) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    icao,
                    session_id,
                    ts,
                    data_["lat"],
                    data_["lon"],
                    data_["altitude_ft"],
                    data_["speed_kt"],
                    data_["track_deg"],
                    data_["callsign"],
                ),
            )

    def _on_new_aircraft(self, icao):
        logger.info("新規機体を検出しました: %s", icao)
        notify_sound.play_new_aircraft_sound()

        def _enrich():
            with self._lock:
                ac = self._aircraft.get(icao)
                callsign = ac["callsign"] if ac else ""
            info = aircraft_enrich.enrich(icao, callsign)
            with self._lock:
                if icao in self._aircraft:
                    self._aircraft[icao]["info"] = info

        threading.Thread(target=_enrich, daemon=True).start()

    def get_current_aircraft(self):
        with self._lock:
            return list(self._aircraft.values())
