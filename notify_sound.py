"""新規機体検出時の通知音再生。

Raspi5側ではMAX98357A(I2Sアンプ、card 2)経由でaplayにより再生する想定
（doll-ai/OpenClawの音声アシスタントモジュールと同じサウンドカードを共用）。
再生コマンドが無い/失敗する開発環境ではログ出力のみのno-opにフォールバックする。
"""
import logging
import shutil
import subprocess
import threading

import config

logger = logging.getLogger(__name__)

_aplay_path = shutil.which("aplay")


def play_new_aircraft_sound():
    if not config.NOTIFY_SOUND_ENABLED:
        return
    threading.Thread(target=_play, daemon=True).start()


def _play():
    if _aplay_path is None:
        logger.info("（通知音no-op: aplayが見つかりません）新規機体検出音")
        return
    try:
        subprocess.run(
            [_aplay_path, config.NOTIFY_SOUND_PATH],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("通知音の再生に失敗しました: %s", exc)
