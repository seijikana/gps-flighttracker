import logging

from flask import Flask, jsonify, send_from_directory

import adsb_tracker
import config
import db
import gps_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static", static_url_path="/static")

db.init_db()
db.start_periodic_checkpoint()
session_manager = gps_session.SessionManager()
tracker = adsb_tracker.AdsbTracker(session_id_provider=session_manager.current_session_id)


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/tiles/<path:filename>")
def tiles(filename):
    """日本全国分のオフラインタイル（USB HDD等のconfig.TILE_PATH配下）を配信する。"""
    return send_from_directory(config.TILE_PATH, filename)


@app.route("/api/sessions")
def api_sessions():
    with db.cursor() as cur:
        cur.execute(
            "SELECT s.id, s.started_at, s.ended_at, s.is_active, COUNT(p.id) AS point_count "
            "FROM sessions s LEFT JOIN session_points p ON p.session_id = s.id "
            "GROUP BY s.id ORDER BY s.started_at DESC"
        )
        rows = [dict(row) for row in cur.fetchall()]
    return jsonify(rows)


@app.route("/api/sessions/<int:session_id>/track")
def api_session_track(session_id):
    with db.cursor() as cur:
        cur.execute(
            "SELECT ts, lat, lon, speed_kmh, heading FROM session_points "
            "WHERE session_id = ? ORDER BY ts ASC",
            (session_id,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    return jsonify(rows)


@app.route("/api/sessions/current")
def api_current_session():
    session_id = session_manager.current_session_id()
    if session_id is None:
        return jsonify({"session_id": None, "points": []})
    with db.cursor() as cur:
        cur.execute(
            "SELECT ts, lat, lon, speed_kmh, heading FROM session_points "
            "WHERE session_id = ? ORDER BY ts ASC",
            (session_id,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    return jsonify({"session_id": session_id, "points": rows})


@app.route("/api/aircraft")
def api_aircraft():
    aircraft_list = tracker.get_current_aircraft()
    result = []
    for ac in aircraft_list:
        # 位置情報が無い機体（Mode-S/ident信号のみでADS-B位置が未取得）は地図上に表示できないため除外する
        if ac.get("lat") is None or ac.get("lon") is None:
            continue
        item = dict(ac)
        if item.get("speed_kt") is not None:
            item["speed_kmh"] = item["speed_kt"] * 1.852
        if item.get("altitude_ft") is not None:
            item["altitude_m"] = item["altitude_ft"] * 0.3048
        info = item.pop("info", None) or {}
        item["airline"] = info.get("airline")
        item["country"] = info.get("country")
        item["country_flag"] = info.get("country_flag")
        item["aircraft_type"] = info.get("aircraft_type")
        item["origin"] = info.get("origin")
        item["origin_flag"] = info.get("origin_flag")
        item["destination"] = info.get("destination")
        item["destination_flag"] = info.get("destination_flag")
        item["photo_url"] = info.get("photo_url")
        result.append(item)
    return jsonify(result)


def main():
    session_manager.start()
    tracker.start()
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG, use_reloader=False)


if __name__ == "__main__":
    main()
