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

    _MOCK_TYPE_CODES = ["B738", "B77W", "B789", "A320", "A321", "A359", "B763", "A333"]

    def _fetch_mock(self):
        if self._mock_state is None:
            self._mock_state = [
                {"hex": "abc123", "flight": "ANA123", "lat": 34.70, "lon": 135.50, "alt": 32000, "spd": 420, "track": 90, "t": "B738"},
                {"hex": "def456", "flight": "JAL456", "lat": 34.65, "lon": 135.55, "alt": 28000, "spd": 380, "track": 270, "t": "B789"},
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
                    "t": random.choice(self._MOCK_TYPE_CODES),
                }
            )
        return list(self._mock_state)

    def _update(self, aircraft_list):
        now = time.time()
        seen_icaos = set()
        new_icaos = []

        callsign_resolved_icaos = []

        with self._lock:
            for ac in aircraft_list:
                icao = ac.get("hex")
                if not icao:
                    continue
                seen_icaos.add(icao)
                if icao not in self._aircraft:
                    new_icaos.append(icao)
                # 既存機体の場合は補完済みの"info"を保持し、位置等のみ更新する
                # （毎ポーリングで丸ごと上書きするとaircraft_enrichの非同期結果が消えてしまう）
                existing = self._aircraft.get(icao, {})
                existing_info = existing.get("info")
                new_callsign = (ac.get("flight") or "").strip()
                # 初回検出時はコールサインが未確定（空文字）で補完を試みており、その時点では
                # 出発地/目的地・運航会社を取得できない。コールサインが後から確定した時点で
                # 再度補完を試みる（新規機体でない場合のみ。新規はnew_icaosで既に処理される）。
                if icao in self._aircraft and not existing.get("callsign") and new_callsign:
                    callsign_resolved_icaos.append(icao)
                # readsb/dump1090-faは"alt_baro"/"gs"、古いdump1090系は"alt"/"spd"を使うため両対応する。
                # alt_baroは地上にいる機体では数値ではなく"ground"文字列になるため数値以外は除外する。
                alt_raw = ac.get("alt_baro", ac.get("alt"))
                altitude_ft = alt_raw if isinstance(alt_raw, (int, float)) else None
                # Mode-S応答はあるがADS-B位置フレームをまだ受信していないポーリングでは
                # lat/lonがNoneになることがある。その場合に位置をNoneで上書きすると地図上から
                # 即座に消えてしまうため、タイムアウト（3分）まで直前の既知位置を保持する。
                new_lat = ac.get("lat")
                new_lon = ac.get("lon")
                lat = new_lat if new_lat is not None else existing.get("lat")
                lon = new_lon if new_lon is not None else existing.get("lon")
                self._aircraft[icao] = {
                    "icao": icao,
                    "callsign": new_callsign,
                    "lat": lat,
                    "lon": lon,
                    "altitude_ft": altitude_ft,
                    "speed_kt": ac.get("gs", ac.get("spd")),
                    "track_deg": ac.get("track"),
                    "type_code": ac.get("t"),
                    "last_seen": now,
                    "info": existing_info,
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
        for icao in callsign_resolved_icaos:
            self._trigger_enrich(icao)

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
        self._trigger_enrich(icao)

    def _trigger_enrich(self, icao):
        """機体情報補完をバックグラウンドで実行する。

        新規機体検出時に加え、検出時点ではコールサインが未確定で出発地/目的地・
        運航会社が取得できなかった機体について、コールサインが後から確定した際の
        再試行にも使う。
        """

        def _enrich():
            with self._lock:
                ac = self._aircraft.get(icao)
                callsign = ac["callsign"] if ac else ""
                type_code = ac.get("type_code") if ac else None
            try:
                info = aircraft_enrich.enrich(icao, callsign, type_code=type_code)
            except Exception:
                logger.exception("機体情報補完中に予期しないエラーが発生しました: %s", icao)
                info = None
            with self._lock:
                if icao in self._aircraft:
                    self._aircraft[icao]["info"] = info

        threading.Thread(target=_enrich, daemon=True).start()

    def get_current_aircraft(self):
        with self._lock:
            return list(self._aircraft.values())
