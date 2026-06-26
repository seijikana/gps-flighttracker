(function () {
  "use strict";

  // 日本全国分のオフラインタイル（USB HDD等にダウンロード済みのものを /tiles で配信）。
  // タイルが無い場合はOpenStreetMapの公開タイルにフォールバックする（開発・オンライン時用）。
  var OFFLINE_TILE_URL = "/tiles/{z}/{x}/{y}.png";
  var ONLINE_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";

  var map = L.map("map", { zoomControl: true }).setView([34.6937, 135.5023], 12);

  var offlineLayer = L.tileLayer(OFFLINE_TILE_URL, { maxZoom: 18, errorTileUrl: "" });
  var onlineLayer = L.tileLayer(ONLINE_TILE_URL, {
    maxZoom: 18,
    attribution: "&copy; OpenStreetMap contributors",
  });
  offlineLayer.addTo(map);

  // オフラインタイルが一定数404になったらオンラインタイルに切り替える簡易フォールバック
  var offlineTileErrors = 0;
  offlineLayer.on("tileerror", function () {
    offlineTileErrors += 1;
    if (offlineTileErrors > 5 && map.hasLayer(offlineLayer)) {
      map.removeLayer(offlineLayer);
      onlineLayer.addTo(map);
    }
  });

  var followEnabled = true;
  var currentSessionId = null;
  // 自車の走行軌跡は赤の太線、現在地は車のアイコンで表示する
  var CAR_COLOR = "#ff2222";
  var currentPolyline = L.polyline([], { color: CAR_COLOR, weight: 6 }).addTo(map);
  var carIcon = L.divIcon({
    className: "car-icon",
    html: '<span style="color:' + CAR_COLOR + '">🚗</span>',
    iconSize: [48, 48],
    iconAnchor: [24, 24],
  });
  var currentMarker = L.marker([0, 0], { icon: carIcon });
  var historyPolyline = null;
  var aircraftMarkers = {}; // icao -> L.Marker
  var aircraftTrails = {}; // icao -> L.Polyline（前回位置と今回位置を直線で繋いだ軌跡）
  var aircraftColors = {}; // icao -> 割り当てられた色（アイコン/軌跡/リスト欄で共通）
  var aircraftLastPos = {}; // icao -> 直前のlatlng（進行方向ベクトル計算用）
  var aircraftHeadings = {}; // icao -> 直前→現在のベクトルから算出した進行方向(度)

  // 2点間の方位角（北=0度、時計回り）を計算する
  function bearingDeg(from, to) {
    var lat1 = (from[0] * Math.PI) / 180;
    var lat2 = (to[0] * Math.PI) / 180;
    var dLon = ((to[1] - from[1]) * Math.PI) / 180;
    var y = Math.sin(dLon) * Math.cos(lat2);
    var x = Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon);
    return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
  }

  // 機体ごとに視認しやすい色を巡回割り当てする
  var COLOR_PALETTE = [
    "#ff4e4e", "#4ea1ff", "#4eff8f", "#ffd24e", "#c44eff",
    "#ff8c4e", "#4effe9", "#ff4ea1", "#9bff4e", "#4e6bff",
  ];
  var nextColorIndex = 0;

  function colorForAircraft(icao) {
    if (!aircraftColors[icao]) {
      aircraftColors[icao] = COLOR_PALETTE[nextColorIndex % COLOR_PALETTE.length];
      nextColorIndex += 1;
    }
    return aircraftColors[icao];
  }

  var followBtn = document.getElementById("follow-toggle");
  followBtn.addEventListener("click", function () {
    followEnabled = !followEnabled;
    followBtn.classList.toggle("active", followEnabled);
    followBtn.textContent = "自動追従: " + (followEnabled ? "ON" : "OFF");
  });

  var toggleBtn = document.getElementById("toggle-panel");
  var sessionListEl = document.getElementById("session-list");
  toggleBtn.addEventListener("click", function () {
    sessionListEl.classList.toggle("open");
  });

  function fetchJson(url) {
    return fetch(url).then(function (resp) {
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.json();
    });
  }

  // 元のサイズ(20x20)から5倍(100x100)に拡大し、機体ごとの色・進行方向を反映したアイコンを作る。
  // headingDeg: 軌跡の方向（直前→現在のベクトル）。✈グリフは右上(NE/45度)を向いて
  // デザインされていることが多いため、北(0度)を正面とみなして-45度補正する。
  function aircraftIcon(color, headingDeg) {
    var rotate = (headingDeg || 0) - 45;
    return L.divIcon({
      className: "aircraft-icon",
      html:
        '<span style="color:' +
        color +
        "; display:inline-block; transform: rotate(" +
        rotate +
        'deg)">✈</span>',
      iconSize: [100, 100],
      iconAnchor: [50, 50],
    });
  }

  function refreshCurrentSession() {
    fetchJson("/api/sessions/current")
      .then(function (data) {
        currentSessionId = data.session_id;
        var latlngs = data.points.map(function (p) {
          return [p.lat, p.lon];
        });
        currentPolyline.setLatLngs(latlngs);
        if (latlngs.length > 0) {
          var last = latlngs[latlngs.length - 1];
          currentMarker.setLatLng(last);
          if (!map.hasLayer(currentMarker)) currentMarker.addTo(map);
          if (followEnabled) {
            map.panTo(last, { animate: true });
          }
        }
      })
      .catch(function (err) {
        console.warn("current session fetch failed", err);
      });
  }

  function refreshAircraft() {
    fetchJson("/api/aircraft")
      .then(function (list) {
        var seen = {};
        list.forEach(function (ac) {
          if (ac.lat == null || ac.lon == null) return;
          seen[ac.icao] = true;
          var color = colorForAircraft(ac.icao);
          var label =
            (ac.callsign || ac.icao) +
            (ac.airline ? " / " + ac.airline : "") +
            (ac.aircraft_type ? " / " + ac.aircraft_type : "");
          var latlng = [ac.lat, ac.lon];

          // 直前位置からのベクトルで進行方向を更新（ほぼ同一点の場合は前回の向きを保持）
          var prevPos = aircraftLastPos[ac.icao];
          if (prevPos && (prevPos[0] !== latlng[0] || prevPos[1] !== latlng[1])) {
            aircraftHeadings[ac.icao] = bearingDeg(prevPos, latlng);
          }
          var heading = aircraftHeadings[ac.icao] || 0;
          aircraftLastPos[ac.icao] = latlng;

          if (!aircraftMarkers[ac.icao]) {
            aircraftMarkers[ac.icao] = L.marker(latlng, { icon: aircraftIcon(color, heading) })
              .addTo(map)
              .bindTooltip(label);
          } else {
            aircraftMarkers[ac.icao].setLatLng(latlng);
            aircraftMarkers[ac.icao].setIcon(aircraftIcon(color, heading));
            aircraftMarkers[ac.icao].setTooltipContent(label);
          }

          // 前回位置と今回位置を直線で繋いで軌跡として残す
          if (!aircraftTrails[ac.icao]) {
            aircraftTrails[ac.icao] = L.polyline([latlng], { color: color, weight: 3 }).addTo(map);
          } else {
            aircraftTrails[ac.icao].addLatLng(latlng);
          }
        });
        // 現在検出されない機体はマーカー・軌跡とも消す
        Object.keys(aircraftMarkers).forEach(function (icao) {
          if (!seen[icao]) {
            map.removeLayer(aircraftMarkers[icao]);
            delete aircraftMarkers[icao];
            if (aircraftTrails[icao]) {
              map.removeLayer(aircraftTrails[icao]);
              delete aircraftTrails[icao];
            }
            delete aircraftColors[icao];
            delete aircraftLastPos[icao];
            delete aircraftHeadings[icao];
          }
        });
        renderAircraftPanel(list);
      })
      .catch(function (err) {
        console.warn("aircraft fetch failed", err);
      });
  }

  function renderAircraftPanel(list) {
    var panel = document.getElementById("aircraft-info");
    if (list.length === 0) {
      panel.classList.add("hidden");
      return;
    }
    panel.classList.remove("hidden");
    panel.innerHTML = list
      .map(function (ac) {
        var color = colorForAircraft(ac.icao);
        var alt = ac.altitude_m != null ? Math.round(ac.altitude_m) + " m" : "-";
        var spd = ac.speed_kmh != null ? Math.round(ac.speed_kmh) + " km/h" : "-";
        var airlineCountry = [ac.airline, ac.country].filter(Boolean).join(" / ");

        // 各行を<div>で独立したブロックにする（<br>とdisplay:blockを併用すると
        // 行間が二重になるため、改行は全てdiv境界のみで行う）
        var lines = [
          "<div><strong>" + (ac.callsign || ac.icao) + "</strong>" + (airlineCountry ? " / " + airlineCountry : "") + "</div>",
        ];
        if (ac.aircraft_type) {
          lines.push("<div>" + ac.aircraft_type + "</div>");
        }
        lines.push("<div>高度: " + alt + " / 速度: " + spd + "</div>");
        if (ac.origin) {
          lines.push('<div class="route-line">発: ' + ac.origin + "</div>");
        }
        if (ac.destination) {
          lines.push('<div class="route-line">→ 着: ' + ac.destination + "</div>");
        }

        return (
          '<div class="aircraft-row" style="border-left-color:' +
          color +
          "; color:" +
          color +
          '">' +
          lines.join("") +
          "</div>"
        );
      })
      .join("");
  }

  function loadSessionList() {
    fetchJson("/api/sessions").then(function (sessions) {
      sessionListEl.innerHTML = "";
      sessions.forEach(function (s) {
        var div = document.createElement("div");
        div.className = "session-item" + (s.is_active ? " active-session" : "");
        var start = new Date(s.started_at * 1000).toLocaleString();
        div.textContent = (s.is_active ? "[走行中] " : "") + start + "（" + s.point_count + "点）";
        div.addEventListener("click", function () {
          showHistorySession(s.id);
        });
        sessionListEl.appendChild(div);
      });
    });
  }

  function showHistorySession(sessionId) {
    fetchJson("/api/sessions/" + sessionId + "/track").then(function (points) {
      var latlngs = points.map(function (p) {
        return [p.lat, p.lon];
      });
      if (historyPolyline) {
        map.removeLayer(historyPolyline);
      }
      historyPolyline = L.polyline(latlngs, { color: "#ff9f4e", weight: 3, dashArray: "6 6" }).addTo(map);
      if (latlngs.length > 0) {
        map.fitBounds(historyPolyline.getBounds());
      }
    });
  }

  refreshCurrentSession();
  refreshAircraft();
  loadSessionList();
  setInterval(refreshCurrentSession, 3000);
  setInterval(refreshAircraft, 3000);
  setInterval(loadSessionList, 30000);
})();
