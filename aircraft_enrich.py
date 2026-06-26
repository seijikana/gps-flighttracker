"""ICAO24/コールサインから機種・エアライン・出発地/目的地を補完する。

- 機種: ローカルにキャッシュしたOpenSky aircraftDatabase.csv（config.AIRCRAFT_DB_CSV_PATH）を参照。
- エアライン・出発地/目的地: OpenSky Network REST APIから取得。
- オフライン時（通信不可）は補完をスキップし、Noneを返す（呼び出し側はdump1090の情報のみで表示する）。

レート制限・利用規約に注意。OpenSky/FlightRadar24のAPI仕様は変更される可能性があるため、
実運用前に最新の利用規約・エンドポイントを確認すること。
"""
import csv
import logging
import os
import time

import requests

import config
import db

logger = logging.getLogger(__name__)

_aircraft_type_db = None  # icao24 -> aircraft_type の辞書（遅延ロード）


def _load_aircraft_type_db():
    global _aircraft_type_db
    if _aircraft_type_db is not None:
        return _aircraft_type_db
    _aircraft_type_db = {}
    path = config.AIRCRAFT_DB_CSV_PATH
    if not path or not os.path.exists(path):
        logger.info("機体データベースCSVが設定されていないため機種補完は無効です")
        return _aircraft_type_db
    with open(path, encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        for row in reader:
            icao24 = (row.get("icao24") or "").strip().lower()
            model = (row.get("model") or row.get("typecode") or "").strip()
            if icao24 and model:
                _aircraft_type_db[icao24] = model
    logger.info("機体データベースを読み込みました（%d件）", len(_aircraft_type_db))
    return _aircraft_type_db


def _get_cached(icao):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM aircraft_info_cache WHERE icao = ?", (icao,))
        row = cur.fetchone()
    if row is None:
        return None
    if time.time() - row["fetched_at"] > config.ENRICH_CACHE_TTL_SEC:
        return None
    return dict(row)


def _save_cache(icao, info):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO aircraft_info_cache "
            "(icao, callsign, airline, aircraft_type, origin, destination, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(icao) DO UPDATE SET "
            "callsign=excluded.callsign, airline=excluded.airline, aircraft_type=excluded.aircraft_type, "
            "origin=excluded.origin, destination=excluded.destination, fetched_at=excluded.fetched_at",
            (
                icao,
                info.get("callsign"),
                info.get("airline"),
                info.get("aircraft_type"),
                info.get("origin"),
                info.get("destination"),
                time.time(),
            ),
        )


def _fetch_route_from_opensky(callsign):
    """OpenSky Network のflights/aircraftやroutes系APIから出発地/目的地を推定する。

    OpenSky側の正式なroute検索APIは認証や時間範囲指定を要するため、ここでは
    シンプルな例として失敗時はNoneを返すフォールバックのみ実装する。実運用では
    認証情報・エンドポイントの確認が必要。
    """
    if not callsign:
        return None, None
    try:
        resp = requests.get(
            f"{config.OPENSKY_API_BASE}/routes",
            params={"callsign": callsign},
            timeout=config.ENRICH_TIMEOUT_SEC,
        )
        if resp.status_code != 200:
            return None, None
        data = resp.json()
        route = data.get("route") or []
        if len(route) >= 2:
            return route[0], route[-1]
    except requests.RequestException as exc:
        logger.debug("OpenSky route取得に失敗（オフライン想定）: %s", exc)
    return None, None


def _guess_airline_from_callsign(callsign):
    """コールサイン先頭3文字（ICAO航空会社コード）から簡易的にエアライン名を推定する。"""
    if not callsign or len(callsign) < 3:
        return None
    prefix = callsign[:3].upper()
    known = {
        "ANA": "All Nippon Airways",
        "JAL": "Japan Airlines",
        "SKY": "Skymark Airlines",
        "APJ": "Peach Aviation",
        "JJP": "Jetstar Japan",
        "UAL": "United Airlines",
        "DAL": "Delta Air Lines",
        "AAL": "American Airlines",
    }
    return known.get(prefix)


def enrich(icao, callsign):
    """機体情報を補完する。通信不可時はNoneを返し、呼び出し側はdump1090情報のみで表示する。"""
    icao = (icao or "").lower()
    cached = _get_cached(icao)
    if cached is not None:
        return cached

    aircraft_type = _load_aircraft_type_db().get(icao)
    origin, destination = _fetch_route_from_opensky(callsign)
    airline = _guess_airline_from_callsign(callsign)

    if aircraft_type is None and origin is None and airline is None:
        # 何も補完できなかった（オフライン等）場合はキャッシュせず、次回再試行できるようにする
        return None

    info = {
        "callsign": callsign,
        "airline": airline,
        "aircraft_type": aircraft_type,
        "origin": origin,
        "destination": destination,
    }
    _save_cache(icao, info)
    return info
