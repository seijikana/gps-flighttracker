(function () {
  "use strict";

  // 日本全国分のオフラインタイル（USB HDD等にダウンロード済みのものを /tiles で配信）。
  // タイルが無い場合はOpenStreetMapの公開タイルにフォールバックする（開発・オンライン時用）。
  var OFFLINE_TILE_URL = "/tiles/{z}/{x}/{y}.png";
  var ONLINE_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";

  var map = L.map("map", { zoomControl: true }).setView([34.6937, 135.5023], 12);

  // 地図の距離ゲージ（スケールバー、左下に表示。メートル法のみで十分なため imperial は無効化）
  L.control.scale({ position: "bottomleft", metric: true, imperial: false }).addTo(map);

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
        var countryWithFlag = [flagImg(ac.country_flag), ac.country].filter(Boolean).join(" ");
        var airlineCountry = [ac.airline, countryWithFlag].filter(Boolean).join(" / ");

        // 各行を<div>で独立したブロックにする（<br>とdisplay:blockを併用すると
        // 行間が二重になるため、改行は全てdiv境界のみで行う）
        var dist = ac.distance_km != null ? ac.distance_km.toFixed(1) + " km" : null;
        var altSpdType = [dist ? "距離: " + dist : null, "高度: " + alt, "速度: " + spd, ac.aircraft_type]
          .filter(Boolean)
          .join(" / ");

        var lines = [
          "<div><strong>" + (ac.callsign || ac.icao) + "</strong>" + (airlineCountry ? " / " + airlineCountry : "") + "</div>",
          "<div>" + altSpdType + "</div>",
        ];
        if (ac.origin) {
          var originWithFlag = [flagImg(ac.origin_flag), ac.origin].filter(Boolean).join(" ");
          lines.push('<div class="route-line">発: ' + originWithFlag + "</div>");
        }
        if (ac.destination) {
          var destWithFlag = [flagImg(ac.destination_flag), ac.destination].filter(Boolean).join(" ");
          lines.push('<div class="route-line">→ 着: ' + destWithFlag + "</div>");
        }

        var textHtml = '<div class="aircraft-row-text">' + lines.join("") + "</div>";
        var photoHtml = ac.photo_url
          ? '<img class="aircraft-row-photo" src="' + ac.photo_url + '" alt="" loading="lazy">'
          : "";

        return (
          '<div class="aircraft-row" style="border-left-color:' +
          color +
          "; color:" +
          color +
          '">' +
          textHtml +
          photoHtml +
          "</div>"
        );
      })
      .join("");
  }

  // 国コード(ISO2小文字、例: "jp")からstatic/flags配下のSVG国旗アイコンを生成する
  function flagImg(iso2) {
    if (!iso2) return "";
    return '<img class="flag-icon" src="/static/flags/' + iso2 + '.svg" alt="" loading="lazy">';
  }

  // 日付ごとのセッション軌跡オーバーレイ（チェックボックスでON/OFF、複数日同時表示可）
  var DATE_COLOR_PALETTE = [
    "#ff9f4e", "#4ea1ff", "#4eff8f", "#ffd24e", "#c44eff",
    "#ff4e4e", "#4effe9", "#ff4ea1", "#9bff4e", "#4e6bff",
  ];
  var dateColors = {}; // date(YYYY-MM-DD) -> 色
  var dateLayers = {}; // date -> L.LayerGroup（チェックを外すと地図から除去）
  var nextDateColorIndex = 0;

  function colorForDate(date) {
    if (!dateColors[date]) {
      dateColors[date] = DATE_COLOR_PALETTE[nextDateColorIndex % DATE_COLOR_PALETTE.length];
      nextDateColorIndex += 1;
    }
    return dateColors[date];
  }

  function loadSessionList() {
    fetchJson("/api/sessions/by_date").then(function (days) {
      sessionListEl.innerHTML = "";
      days.forEach(function (d) {
        var color = colorForDate(d.date);
        var row = document.createElement("label");
        row.className = "session-item date-item";
        row.style.borderLeft = "6px solid " + color;

        var checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = !!dateLayers[d.date];
        checkbox.addEventListener("change", function () {
          if (checkbox.checked) {
            showDateTrack(d.date);
          } else {
            hideDateTrack(d.date);
          }
        });

        var text = document.createElement("span");
        text.textContent = " " + d.date + "（" + d.session_ids.length + "回・" + d.point_count + "点）";

        row.appendChild(checkbox);
        row.appendChild(text);
        sessionListEl.appendChild(row);
      });
    });
  }

  function showDateTrack(date) {
    fetchJson("/api/sessions/track_by_date/" + date).then(function (tracks) {
      var color = colorForDate(date);
      var group = L.layerGroup();
      var allLatLngs = [];
      tracks.forEach(function (t) {
        var latlngs = t.points.map(function (p) {
          return [p.lat, p.lon];
        });
        if (latlngs.length === 0) return;
        L.polyline(latlngs, { color: color, weight: 4 }).addTo(group);
        allLatLngs = allLatLngs.concat(latlngs);
      });
      group.addTo(map);
      dateLayers[date] = group;
      if (allLatLngs.length > 0) {
        map.fitBounds(L.latLngBounds(allLatLngs));
      }
    });
  }

  function hideDateTrack(date) {
    if (dateLayers[date]) {
      map.removeLayer(dateLayers[date]);
      delete dateLayers[date];
    }
  }

  refreshCurrentSession();
  refreshAircraft();
  loadSessionList();
  setInterval(refreshCurrentSession, 3000);
  setInterval(refreshAircraft, 3000);
  setInterval(loadSessionList, 30000);
})();
