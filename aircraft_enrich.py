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
            "(icao, callsign, airline, country, aircraft_type, origin, destination, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(icao) DO UPDATE SET "
            "callsign=excluded.callsign, airline=excluded.airline, country=excluded.country, "
            "aircraft_type=excluded.aircraft_type, "
            "origin=excluded.origin, destination=excluded.destination, fetched_at=excluded.fetched_at",
            (
                icao,
                info.get("callsign"),
                info.get("airline"),
                info.get("country"),
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
    except (requests.RequestException, ValueError) as exc:
        # ValueError: resp.json()がJSONとして解釈できないレスポンス（オフライン時のプロキシ応答等）
        logger.debug("OpenSky route取得に失敗（オフライン想定）: %s", exc)
    return None, None


_COUNTRY_RANGES = [
    (0x800000, 0x83FFFF, "韓国"),
    (0x840000, 0x87FFFF, "日本"),
    (0x880000, 0x88FFFF, "タイ"),
    (0x900000, 0x9FFFFF, "インド"),
    (0x780000, 0x7BFFFF, "中国"),
    (0xA00000, 0xAFFFFF, "アメリカ"),
    (0xC00000, 0xC3FFFF, "カナダ"),
    (0x3C0000, 0x3FFFFF, "ドイツ"),
    (0x380000, 0x3BFFFF, "フランス"),
    (0x400000, 0x43FFFF, "イギリス"),
    (0x7C0000, 0x7FFFFF, "オーストラリア"),
]


def icao24_country(icao):
    """ICAO24アドレスの先頭ビット割り当て範囲から所属国を簡易判定する（日本語名で返す）。

    ICAO Annex 10で国別にアドレス範囲が割り当てられている。主要国のみの
    簡易テーブルであり、未知の範囲はNoneを返す。
    """
    if not icao:
        return None
    try:
        value = int(icao, 16)
    except ValueError:
        return None
    for low, high, name in _COUNTRY_RANGES:
        if low <= value <= high:
            return name
    return None


_AIRLINE_JA = {
    "ANA": "全日空(ANA)",
    "JAL": "日本航空(JAL)",
    "SKY": "スカイマーク(SKY)",
    "APJ": "ピーチ・アビエーション(APJ)",
    "JJP": "ジェットスター・ジャパン(JJP)",
    "UAL": "ユナイテッド航空(UAL)",
    "DAL": "デルタ航空(DAL)",
    "AAL": "アメリカン航空(AAL)",
}


def _guess_airline_from_callsign(callsign):
    """コールサイン先頭3文字（ICAO航空会社コード）から簡易的にエアライン名（日本語）を推定する。"""
    if not callsign or len(callsign) < 3:
        return None
    return _AIRLINE_JA.get(callsign[:3].upper())


# 国内主要空港: ICAOコード -> (空港名, 県名)
_DOMESTIC_AIRPORTS = {
    "RJAA": ("成田国際空港", "千葉県"),
    "RJTT": ("東京国際空港(羽田)", "東京都"),
    "RJBB": ("関西国際空港", "大阪府"),
    "RJOO": ("大阪国際空港(伊丹)", "大阪府"),
    "RJGG": ("中部国際空港", "愛知県"),
    "RJFF": ("福岡空港", "福岡県"),
    "ROAH": ("那覇空港", "沖縄県"),
    "RJCC": ("新千歳空港", "北海道"),
    "RJOA": ("広島空港", "広島県"),
    "RJNK": ("小松空港", "石川県"),
}

# 主要海外空港: ICAOコード -> (都市名, 国名)
_FOREIGN_AIRPORTS = {
    "KIAH": ("ヒューストン", "アメリカ"),
    "KJFK": ("ニューヨーク", "アメリカ"),
    "KLAX": ("ロサンゼルス", "アメリカ"),
    "EGLL": ("ロンドン", "イギリス"),
    "ZBAA": ("北京", "中国"),
    "RKSI": ("ソウル", "韓国"),
    "VHHH": ("香港", "中国"),
    "WSSS": ("シンガポール", "シンガポール"),
}


def format_airport(code):
    """空港コードを「空港名(県名)」（国内）または「国名(都市)」（海外）の表記に変換する。

    未収録の空港コードは元のコードをそのまま返す（簡易テーブルのため要拡充）。
    """
    if not code:
        return None
    code = code.upper()
    if code in _DOMESTIC_AIRPORTS:
        name, pref = _DOMESTIC_AIRPORTS[code]
        return f"{name}({pref})"
    if code in _FOREIGN_AIRPORTS:
        city, country = _FOREIGN_AIRPORTS[code]
        return f"{country}({city})"
    return code


# ICAO機種コード -> 日本語の機種名
_AIRCRAFT_TYPE_JA = {
    "B738": "ボーイング737-800",
    "B739": "ボーイング737-900",
    "B77W": "ボーイング777-300ER",
    "B772": "ボーイング777-200",
    "B789": "ボーイング787-9",
    "B788": "ボーイング787-8",
    "B763": "ボーイング767-300",
    "A320": "エアバスA320",
    "A321": "エアバスA321",
    "A359": "エアバスA350-900",
    "A333": "エアバスA330-300",
}


def format_aircraft_type(code):
    if not code:
        return None
    return _AIRCRAFT_TYPE_JA.get(code.upper(), code)


def enrich(icao, callsign, type_code=None):
    """機体情報を補完する。通信不可時はNoneを返し、呼び出し側はdump1090情報のみで表示する。

    type_code: dump1090/readsbが提供する機種コード（aircraft.jsonの"t"フィールド等）。
    機体データベースCSVが無い/該当しない場合のフォールバックとして使用する。
    """
    icao = (icao or "").lower()
    cached = _get_cached(icao)
    # 初回検出時はコールサイン未確定（空文字）でキャッシュされることが多く、
    # 後でコールサインが確定してもキャッシュが古いままだとエアライン/出発地目的地が
    # 永久にnullになってしまう。現在のコールサインとキャッシュ時のものが異なる場合は
    # キャッシュを無視して再取得する。
    if cached is not None and (not callsign or cached.get("callsign") == callsign):
        return cached

    aircraft_type_code = _load_aircraft_type_db().get(icao) or type_code
    aircraft_type = format_aircraft_type(aircraft_type_code)
    origin_code, destination_code = _fetch_route_from_opensky(callsign)
    origin = format_airport(origin_code)
    destination = format_airport(destination_code)
    airline = _guess_airline_from_callsign(callsign)
    country = icao24_country(icao)  # ICAO24アドレス範囲からの判定はオフラインでも可能

    if aircraft_type is None and origin is None and airline is None and country is None:
        # 何も補完できなかった（オフライン等）場合はキャッシュせず、次回再試行できるようにする
        return None

    info = {
        "callsign": callsign,
        "airline": airline,
        "country": country,
        "aircraft_type": aircraft_type,
        "origin": origin,
        "destination": destination,
    }
    _save_cache(icao, info)
    return info
