"""ICAO24/コールサインから機種・エアライン・出発地/目的地を補完する。

- 出発地/目的地・運航会社: OpenSky Network のroutes APIから毎回ライブ取得する
  （旧metadata/aircraft APIは廃止済み(410 Gone)のため使用不可）。
- 機種: ローカルにキャッシュしたOpenSky aircraftDatabase.csv（config.AIRCRAFT_DB_CSV_PATH）
  またはdump1090/readsbのtype_codeから補完（ライブAPI取得元が無いため）。
- オフライン時（通信不可）はキャッシュ（直近に取得できた値）にフォールバックする。

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
from country_names_ja import COUNTRY_NAME_JA

logger = logging.getLogger(__name__)

_WORLD_AIRPORTS_PATH = os.path.join(os.path.dirname(__file__), "data", "airports_world.csv")
_world_airports_db = None  # code -> (name, country_iso2) の辞書（遅延ロード）


def _load_world_airports_db():
    """OurAirports由来の世界全空港データベース（data/airports_world.csv）を読み込む。

    手動キュレーションの_DOMESTIC_AIRPORTS/_FOREIGN_AIRPORTSでカバーできていない
    空港コードのフォールバックとして使う（「国名(空港名)」形式で表示）。
    """
    global _world_airports_db
    if _world_airports_db is not None:
        return _world_airports_db
    _world_airports_db = {}
    if not os.path.exists(_WORLD_AIRPORTS_PATH):
        logger.warning("世界空港データベースが見つかりません: %s", _WORLD_AIRPORTS_PATH)
        return _world_airports_db
    with open(_WORLD_AIRPORTS_PATH, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            code = (row.get("code") or "").strip().upper()
            name = (row.get("name") or "").strip()
            country = (row.get("country") or "").strip().upper()
            if code and name:
                _world_airports_db[code] = (name, country)
    logger.info("世界空港データベースを読み込みました（%d件）", len(_world_airports_db))
    return _world_airports_db

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
    """OpenSky Network のroutes APIから出発地/目的地・運航会社IATAコードを取得する（毎回ライブ取得）。

    旧"metadata/aircraft/icao/{icao24}"エンドポイントは廃止済み（410 Gone）のため、
    機種情報のライブ取得元としては使えない。routesエンドポイントは現在も稼働しており、
    出発地/目的地に加えて"operatorIata"（運航会社のIATAコード）も得られるため、
    エアラインのライブ判定にも利用する。失敗時（オフライン等）は全てNoneを返す。
    """
    if not callsign:
        return None, None, None
    try:
        resp = requests.get(
            f"{config.OPENSKY_API_BASE}/routes",
            params={"callsign": callsign},
            timeout=config.ENRICH_TIMEOUT_SEC,
        )
        if resp.status_code != 200:
            return None, None, None
        data = resp.json()
        route = data.get("route") or []
        operator_iata = (data.get("operatorIata") or "").strip() or None
        if len(route) >= 2:
            return route[0], route[-1], operator_iata
        return None, None, operator_iata
    except (requests.RequestException, ValueError) as exc:
        # ValueError: resp.json()がJSONとして解釈できないレスポンス（オフライン時のプロキシ応答等）
        logger.debug("OpenSky route取得に失敗（オフライン想定）: %s", exc)
    return None, None, None


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
    "ADO": "AIRDO(ADO)",
    "SFJ": "スターフライヤー(SFJ)",
    "SNJ": "ソラシドエア(SNJ)",
    "IBX": "アイベックスエアラインズ(IBX)",
    "UAL": "ユナイテッド航空(UAL)",
    "DAL": "デルタ航空(DAL)",
    "AAL": "アメリカン航空(AAL)",
    "FDX": "フェデックス(FDX)",
    "CAL": "チャイナエアライン(CAL)",
    "CPA": "キャセイパシフィック航空(CPA)",
    "CSN": "中国南方航空(CSN)",
    "CES": "中国東方航空(CES)",
    "CCA": "中国国際航空(CCA)",
    "KAL": "大韓航空(KAL)",
    "AAR": "アシアナ航空(AAR)",
    "AIC": "インド航空(AIC)",
    "AIQ": "インディゴ(IGO)",
    "TTW": "タイガーエア台湾(TTW)",
    "EVA": "エバー航空(EVA)",
    "SIA": "シンガポール航空(SIA)",
    "THA": "タイ国際航空(THA)",
    "PAL": "フィリピン航空(PAL)",
    "CEB": "セブパシフィック航空(CEB)",
    "VJC": "ベトジェットエア(VJC)",
    "HVN": "ベトナム航空(HVN)",
    "QFA": "カンタス航空(QFA)",
    "GIA": "ガルーダ・インドネシア航空(GIA)",
}


def _guess_airline_from_callsign(callsign):
    """コールサイン先頭3文字（ICAO航空会社コード）から簡易的にエアライン名（日本語）を推定する。"""
    if not callsign or len(callsign) < 3:
        return None
    return _AIRLINE_JA.get(callsign[:3].upper())


# IATA(2文字)航空会社コード -> 日本語名。OpenSky routes APIの"operatorIata"はライブ取得できるため、
# こちらが取得できればコールサイン推定より優先して使う。
_AIRLINE_JA_IATA = {
    "NH": "全日空(ANA)",
    "JL": "日本航空(JAL)",
    "BC": "スカイマーク(SKY)",
    "MM": "ピーチ・アビエーション(APJ)",
    "GK": "ジェットスター・ジャパン(JJP)",
    "UA": "ユナイテッド航空(UAL)",
    "DL": "デルタ航空(DAL)",
    "AA": "アメリカン航空(AAL)",
    "CI": "チャイナエアライン(CAL)",
    "CX": "キャセイパシフィック航空(CPA)",
    "KE": "大韓航空(KAL)",
    "OZ": "アシアナ航空(AAR)",
    "FX": "フェデックス(FDX)",
    "PR": "フィリピン航空(PR)",
    "TR": "スクート(TGW)",
    "5J": "セブパシフィック航空(CEB)",
    "VJ": "ベトジェットエア(VJC)",
    "VN": "ベトナム航空(HVN)",
    "TG": "タイ国際航空(THA)",
    "SQ": "シンガポール航空(SIA)",
    "MU": "中国東方航空(CES)",
    "CZ": "中国南方航空(CSN)",
}


def airline_from_iata(operator_iata):
    if not operator_iata:
        return None
    name = _AIRLINE_JA_IATA.get(operator_iata.upper())
    return name or operator_iata  # 未収録IATAコードはそのまま表示（簡易テーブルのため要拡充）


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
    "RJCH": ("函館空港", "北海道"),
    "RJCK": ("釧路空港", "北海道"),
    "RJSS": ("仙台空港", "宮城県"),
    "RJSK": ("秋田空港", "秋田県"),
    "RJSN": ("新潟空港", "新潟県"),
    "RJSC": ("山形空港", "山形県"),
    "RJAH": ("百里空港(茨城)", "茨城県"),
    "RJSF": ("福島空港", "福島県"),
    "RJOT": ("高松空港", "香川県"),
    "RJOM": ("松山空港", "愛媛県"),
    "RJOK": ("高知空港", "高知県"),
    "RJOH": ("徳島空港", "徳島県"),
    "RJFU": ("長崎空港", "長崎県"),
    "RJFK": ("鹿児島空港", "鹿児島県"),
    "RJFM": ("宮崎空港", "宮崎県"),
    "RJFO": ("大分空港", "大分県"),
    "RJFT": ("熊本空港", "熊本県"),
    "RJFS": ("佐賀空港", "佐賀県"),
    "RJOC": ("美保空港(米子)", "鳥取県"),
    "RJOB": ("岡山空港", "岡山県"),
    "RJBD": ("出雲空港", "島根県"),
    "RJNT": ("富山空港", "富山県"),
    "RJAF": ("福井空港", "福井県"),
    "RJNA": ("名古屋空港(小牧)", "愛知県"),
    "RJTA": ("厚木航空基地", "神奈川県"),
    "RJSA": ("青森空港", "青森県"),
    "RJSM": ("三沢空港", "青森県"),
    "RJSY": ("庄内空港", "山形県"),
    "RJOR": ("岩国空港", "山口県"),
    "RJDC": ("大村空港(長崎)", "長崎県"),
    "ROIG": ("石垣空港", "沖縄県"),
    "ROMY": ("宮古空港", "沖縄県"),
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
    "RPLL": ("マニラ", "フィリピン"),
    "VTBS": ("バンコク", "タイ"),
    "WIII": ("ジャカルタ", "インドネシア"),
    "VVTS": ("ホーチミン", "ベトナム"),
    "ZSPD": ("上海", "中国"),
    "ZGGG": ("広州", "中国"),
}


def format_airport(code):
    """空港コードを表記に変換する。

    1. 手動キュレーション済みの国内空港 -> 「空港名(県名)」
    2. 手動キュレーション済みの主要海外空港 -> 「国名(都市)」
    3. 上記未収録の場合、世界全空港データベース(OurAirports由来)から
       「国名(空港名)」で補完する。
    4. データベースにも無い場合は元のコードをそのまま返す。
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
    world_entry = _load_world_airports_db().get(code)
    if world_entry:
        name, country_iso2 = world_entry
        country_ja = COUNTRY_NAME_JA.get(country_iso2, country_iso2)
        return f"{country_ja}({name})"
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
    """機体情報を補完する。

    OpenSkyから都度取得できる情報（出発地/目的地・運航会社）は毎回ライブ取得を試みる
    （routes API）。キャッシュはオフライン時（通信不可）のフォールバックとしてのみ使い、
    通信可能な間はキャッシュの有無に関わらず常に最新を取得し直す。
    機種は機体メタデータAPIが廃止済み（410 Gone）のためライブ取得できず、ローカルCSV/
    dump1090のtype_codeのみが情報源となる。

    type_code: dump1090/readsbが提供する機種コード（aircraft.jsonの"t"フィールド等）。
    """
    icao = (icao or "").lower()
    cached = _get_cached(icao) or {}

    origin_code, destination_code, operator_iata = _fetch_route_from_opensky(callsign)

    # フィールドごとにライブ値→キャッシュ値の順でフォールバックする。
    # 「ライブ取得が一部失敗したら丸ごとキャッシュを使う」と、country/airlineのような
    # 本来オフラインでも毎回計算できる値まで古いキャッシュに固定されてしまうため、
    # ルート（出発地/目的地）以外は基本的に毎回フレッシュに計算する。
    origin = format_airport(origin_code) or cached.get("origin")
    destination = format_airport(destination_code) or cached.get("destination")

    fallback_type_code = _load_aircraft_type_db().get(icao) or type_code
    aircraft_type = format_aircraft_type(fallback_type_code) or cached.get("aircraft_type")
    airline = (
        airline_from_iata(operator_iata)
        or _guess_airline_from_callsign(callsign)
        or cached.get("airline")
    )
    country = icao24_country(icao) or cached.get("country")  # ICAO24範囲判定はオフラインでも可能

    if aircraft_type is None and origin is None and airline is None and country is None:
        # 何も補完できなかった場合はキャッシュせず、次回再試行できるようにする
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
