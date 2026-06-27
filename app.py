import datetime
import logging
import math

from flask import Flask, jsonify, render_template_string, request, send_from_directory

import adsb_tracker
import config
import db
import gps_session
import settings_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static", static_url_path="/static")

db.init_db()
db.start_periodic_checkpoint()
settings_store.load()
db.start_periodic_idle_purge(settings_store)
session_manager = gps_session.SessionManager()
tracker = adsb_tracker.AdsbTracker(session_id_provider=session_manager.current_session_id)

_SETTINGS_HTML = """
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GPS Tracker 設定</title>
<style>
  :root { --bg: #111; --panel: #1c1c1c; --border: #333; --text: #eee; --muted: #999; --accent: #2a7d2a; }
  * { box-sizing: border-box; }
  body { background: var(--bg); color: var(--text); margin: 0; padding: 1.5em; font-family: sans-serif; }
  h1 { font-size: 1.3em; display: flex; align-items: center; gap: 0.6em; }
  h1 a { font-size: 0.6em; font-weight: normal; color: var(--muted); text-decoration: none; }
  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 1em 1.2em; }
  label { display: block; margin-bottom: 1.2em; font-size: 0.9em; color: var(--muted); }
  label .hint { display: block; font-size: 0.85em; margin-top: 0.2em; }
  input {
    margin-top: 0.4em; font-size: 1em; padding: 0.5em 0.7em; width: 140px;
    background: #0d1117; border: 1px solid var(--border); border-radius: 6px; color: var(--text);
  }
  button {
    margin-top: 0.5em; font-size: 0.95em; padding: 0.6em 1.4em; border-radius: 6px;
    background: var(--accent); color: #fff; border: none; cursor: pointer; font-weight: 600;
  }
  .msg { margin-top: 1em; font-size: 0.9em; }
  .msg.ok { color: #7fdc7f; }
  .msg.err { color: #ff8080; }
</style>
</head>
<body>
  <h1>⚙ GPS Tracker 設定 <a href="/">← 戻る</a></h1>
  <div class="panel">
    <form id="form">
      <label>走行中の軌跡ログ間引き秒数
        <input type="number" step="0.1" name="point_interval_sec" value="{{ s.point_interval_sec }}"> 秒
        <span class="hint">この秒数間隔でのみ走行中の位置をDBに記録する（0.5〜30秒）。
        停車中（速度が低い間）は常に記録しない。短いほど軌跡が滑らかだがDB肥大化が早い</span>
      </label>
      <label>長時間駐車判定の速度
        <input type="number" step="0.1" name="idle_purge_speed_kmh" value="{{ s.idle_purge_speed_kmh }}"> km/h
        <span class="hint">この速度以下が下の継続時間以上続いた区間を自動的に丸ごと削除する
        （信号待ち等の短い停止は対象外）。1〜60km/h</span>
      </label>
      <label>長時間駐車判定の継続時間
        <input type="number" step="0.1" name="idle_purge_duration_hr" value="{{ s.idle_purge_duration_hr }}"> 時間
        <span class="hint">0.1〜48時間。30分ごとに自動チェックし該当区間を削除する</span>
      </label>
      <button type="submit">保存</button>
    </form>
    <div class="msg" id="msg"></div>
  </div>
<script>
document.getElementById('form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target).entries());
  const res = await fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  const d = await res.json();
  const msg = document.getElementById('msg');
  if (d.ok) {
    msg.textContent = '保存しました';
    msg.className = 'msg ok';
  } else {
    msg.textContent = 'エラー: ' + d.error;
    msg.className = 'msg err';
  }
});
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/tiles/<path:filename>")
def tiles(filename):
    """日本全国分のオフラインタイル（USB HDD等のconfig.TILE_PATH配下）を配信する。"""
    return send_from_directory(config.TILE_PATH, filename)


@app.route("/settings")
def settings_page():
    return render_template_string(_SETTINGS_HTML, s=settings_store.get())


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify(settings_store.get())


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    data = request.get_json(force=True)
    ok, err = settings_store.update(data)
    return jsonify({"ok": ok, "error": err})


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


@app.route("/api/sessions/by_date")
def api_sessions_by_date():
    """セッションを開始日（ローカル日付）ごとにグループ化する。地図上の日別チェックボックス表示用。"""
    with db.cursor() as cur:
        cur.execute(
            "SELECT s.id, s.started_at, COUNT(p.id) AS point_count "
            "FROM sessions s LEFT JOIN session_points p ON p.session_id = s.id "
            "GROUP BY s.id ORDER BY s.started_at ASC"
        )
        rows = [dict(row) for row in cur.fetchall()]

    grouped = {}
    for row in rows:
        date_key = datetime.datetime.fromtimestamp(row["started_at"]).strftime("%Y-%m-%d")
        g = grouped.setdefault(date_key, {"date": date_key, "session_ids": [], "point_count": 0})
        g["session_ids"].append(row["id"])
        g["point_count"] += row["point_count"]

    result = sorted(grouped.values(), key=lambda g: g["date"], reverse=True)
    return jsonify(result)


@app.route("/api/sessions/track_by_date/<date>")
def api_track_by_date(date):
    """指定日付（YYYY-MM-DD、ローカル日付）の全セッションの軌跡を、セッションごとに分けて返す。"""
    try:
        day_start = datetime.datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "invalid date, expected YYYY-MM-DD"}), 400

    start_ts = day_start.timestamp()
    end_ts = start_ts + 86400

    with db.cursor() as cur:
        cur.execute(
            "SELECT id FROM sessions WHERE started_at >= ? AND started_at < ? ORDER BY started_at ASC",
            (start_ts, end_ts),
        )
        session_ids = [row["id"] for row in cur.fetchall()]

        tracks = []
        for sid in session_ids:
            cur.execute(
                "SELECT ts, lat, lon, speed_kmh, heading FROM session_points "
                "WHERE session_id = ? ORDER BY ts ASC",
                (sid,),
            )
            points = [dict(row) for row in cur.fetchall()]
            tracks.append({"session_id": sid, "points": points})

    return jsonify(tracks)


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


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@app.route("/api/aircraft")
def api_aircraft():
    aircraft_list = tracker.get_current_aircraft()
    own_pos = session_manager.current_position()
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
        if own_pos is not None:
            item["distance_km"] = round(
                _haversine_km(own_pos[0], own_pos[1], item["lat"], item["lon"]), 2
            )
        else:
            item["distance_km"] = None
        result.append(item)
    # 自車に近い順（距離不明の機体は末尾）にソート
    result.sort(key=lambda ac: (ac["distance_km"] is None, ac["distance_km"]))
    return jsonify(result)


def main():
    session_manager.start()
    tracker.start()
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG, use_reloader=False)


if __name__ == "__main__":
    main()
