# gps-tracker

GPS自車軌跡 + ADS-B航空機トラッキングを同一地図上に表示する車載Webアプリ。
詳細な要件・設計は [CLAUDE.md](CLAUDE.md) を参照。

## セットアップ

```bash
pip install -r requirements.txt
python app.py
```

`http://localhost:5001` で地図画面が表示される。

## 開発・動作確認（ハードウェア未接続環境）

- gpsd / dump1090 に接続できない場合は自動でモックGPS・モックADS-Bにフォールバックする
  （明示的に強制する場合は環境変数 `FORCE_MOCK_GPS=1` / `FORCE_MOCK_ADSB=1`）。
- IMU（MPU-6050, smbus2）が無い場合も自動でモック（振動なし）にフォールバックする。

## 主な環境変数（config.py）

| 変数 | 内容 | デフォルト |
|---|---|---|
| `GPSD_HOST` / `GPSD_PORT` | gpsd接続先 | `localhost:2947` |
| `DUMP1090_URL` | dump1090のaircraft.json URL | `http://localhost:8080/data/aircraft.json` |
| `SESSION_END_STOP_SECONDS` | 走行セッション終了までの停車時間閾値（秒） | `3600`（1時間） |
| `IMU_VIBRATION_THRESHOLD_G` | 振動判定の閾値（G） | `0.05` |
| `AIRCRAFT_DB_CSV_PATH` | OpenSky aircraftDatabase.csvのパス（機種補完用） | 未設定（機種補完無効） |
| `TILE_PATH` | オフライン地図タイル（日本全国, USB HDD等）のディレクトリ | `./tiles` |
| `TRACKER_PORT` | Webサーバーのポート | `5001` |

## 実機（Raspi5）でのデプロイ想定

`raspi5-openclaw`配下のcariot(Flask, port 5000)とはポートを分離し、`TRACKER_PORT=5001`で
別プロセスとして起動する。systemdサービス化・将来のcariotダッシュボード統合は別途検討。
