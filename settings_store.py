"""実行時設定の永続化（dashcamのsettings_store.pyと同じパターン）。

app.py 起動時に load() を呼ぶ。WebUI(/settings) から update() で変更 → 即時反映 + settings.json保存。
"""
import json
import logging
import os
import threading

import config

logger = logging.getLogger(__name__)

_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
_lock = threading.Lock()


def _defaults() -> dict:
    return {
        "point_interval_sec": config.SESSION_POINT_MIN_INTERVAL_SEC,
        # 長時間駐車したまま記録され続けた古い区間の間引き条件
        # （この速度以下が、この時間以上連続したら区間ごと削除する。短い信号待ち等は残る）
        "idle_purge_speed_kmh": 10.0,
        "idle_purge_duration_hr": 2.0,
    }


_state: dict = _defaults()


def load():
    """settings.jsonが存在すれば読み込んでデフォルト値を上書きする。"""
    global _state
    _state = _defaults()
    if not os.path.exists(_FILE):
        return
    try:
        with open(_FILE) as f:
            saved = json.load(f)
        with _lock:
            for k in list(_state):
                if k in saved:
                    _state[k] = float(saved[k])
        logger.info("Settings loaded from %s: %s", _FILE, _state)
    except Exception as e:
        logger.warning("Settings load failed (%s), using defaults", e)


def get() -> dict:
    with _lock:
        return dict(_state)


def update(patch: dict):
    """設定を検証・更新・保存する。戻り値: (ok: bool, error_msg: str)"""
    try:
        interval = float(patch["point_interval_sec"])
        idle_speed = float(patch["idle_purge_speed_kmh"])
        idle_duration = float(patch["idle_purge_duration_hr"])
    except (KeyError, ValueError, TypeError) as e:
        return False, f"パラメータエラー: {e}"

    # 走行中の軌跡記録間引き秒数。0.5秒未満は無制限書き込みに近づきディスク負荷が
    # 増えるため禁止、30秒を超えると時速・カーブ形状の精度が大きく落ちるため禁止。
    if not (0.5 <= interval <= 30.0):
        return False, "ログ間引き秒数は0.5〜30秒の範囲で設定してください"
    if not (1.0 <= idle_speed <= 60.0):
        return False, "長時間駐車判定の速度は1〜60km/hの範囲で設定してください"
    if not (0.1 <= idle_duration <= 48.0):
        return False, "長時間駐車判定の継続時間は0.1〜48時間の範囲で設定してください"

    new = {
        "point_interval_sec": round(interval, 1),
        "idle_purge_speed_kmh": round(idle_speed, 1),
        "idle_purge_duration_hr": round(idle_duration, 2),
    }
    with _lock:
        _state.update(new)
        try:
            with open(_FILE, "w") as f:
                json.dump(_state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error("Settings save failed: %s", e)
            return False, f"保存エラー: {e}"

    logger.info("Settings saved: %s", new)
    return True, ""
