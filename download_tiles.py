#!/usr/bin/env python3
"""オフライン地図タイルの事前ダウンロード（手動/cron実行用、常駐アプリの一部ではない）。

OpenStreetMapの公開タイルサーバー(tile.openstreetmap.org)は大量の自動ダウンロードを
利用規約上禁止しているため、本スクリプトは控えめなレート制限（既定0.5秒/枚）と、
識別可能なUser-Agentを付与して実行する。個人の車載ナビ用途で常用する場合は、
利用規約上問題のない代替タイルソース（例: 自前のタイルサーバー、商用プロバイダのAPIキー利用）
への切り替えを検討すること。

使い方:
  python3 download_tiles.py --dry-run          # ダウンロード量を見積もるだけ
  python3 download_tiles.py --budget-mb 2000   # 容量予算(MB)を指定して実行

容量予算を超えそうになった時点でズームレベルの追加を打ち切る（低ズーム＝広域から優先的に
ダウンロードするため、予算超過時も「広域は粗くだが必ず使えて、自宅周辺など低ズームは
精細に」という優先順位になる）。
"""
import argparse
import math
import os
import sys
import time
import urllib.request

import config

# 既定の対象範囲: 近畿地方一帯（南西=和歌山・南端、北東=琵琶湖・北端付近）
# 必要に応じて --south/--west/--north/--east で上書きすること
DEFAULT_BBOX = {"south": 33.0, "west": 134.5, "north": 35.8, "east": 136.5}

TILE_SERVER = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "raspi5-openclaw-gps-tracker/1.0 (personal in-car navigation, offline tile cache)"

AVG_TILE_BYTES = 15 * 1024  # OSM標準ラスタタイルの平均的なサイズの目安


def deg2num(lat_deg, lon_deg, zoom):
    lat_rad = math.radians(lat_deg)
    n = 2 ** zoom
    xtile = int((lon_deg + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return xtile, ytile


def tiles_for_zoom(bbox, zoom):
    x1, y1 = deg2num(bbox["north"], bbox["west"], zoom)
    x2, y2 = deg2num(bbox["south"], bbox["east"], zoom)
    xmin, xmax = min(x1, x2), max(x1, x2)
    ymin, ymax = min(y1, y2), max(y1, y2)
    for x in range(xmin, xmax + 1):
        for y in range(ymin, ymax + 1):
            yield zoom, x, y


def plan_zoom_levels(bbox, budget_bytes, max_zoom=16):
    """予算内に収まる最大のズームレベルまでを計画する（低ズーム優先）。"""
    plan = []
    total = 0
    for zoom in range(0, max_zoom + 1):
        count = sum(1 for _ in tiles_for_zoom(bbox, zoom))
        size = count * AVG_TILE_BYTES
        if total + size > budget_bytes and plan:
            break
        plan.append((zoom, count, size))
        total += size
        if total > budget_bytes:
            break
    return plan, total


def download_tile(zoom, x, y, dest_dir):
    path = os.path.join(dest_dir, str(zoom), str(x), f"{y}.png")
    if os.path.exists(path):
        return False  # 既にあるのでスキップ（再実行で続きから取得できる）
    os.makedirs(os.path.dirname(path), exist_ok=True)
    url = TILE_SERVER.format(z=zoom, x=x, y=y)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp, open(path, "wb") as f:
        f.write(resp.read())
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--south", type=float, default=DEFAULT_BBOX["south"])
    parser.add_argument("--west", type=float, default=DEFAULT_BBOX["west"])
    parser.add_argument("--north", type=float, default=DEFAULT_BBOX["north"])
    parser.add_argument("--east", type=float, default=DEFAULT_BBOX["east"])
    parser.add_argument("--budget-mb", type=float, default=2000,
                         help="ダウンロード容量予算(MB)。既定2GB（ブートSSDの空き容量を圧迫しない範囲）")
    parser.add_argument("--max-zoom", type=int, default=16)
    parser.add_argument("--rate-sec", type=float, default=0.5,
                         help="タイル1枚あたりの待機秒数（OSM利用規約に配慮したレート制限）")
    parser.add_argument("--dry-run", action="store_true", help="ダウンロードせず計画と見積りのみ表示")
    args = parser.parse_args()

    bbox = {"south": args.south, "west": args.west, "north": args.north, "east": args.east}
    budget_bytes = args.budget_mb * 1024 * 1024

    plan, total_bytes = plan_zoom_levels(bbox, budget_bytes, args.max_zoom)
    print(f"対象範囲: {bbox}")
    print(f"容量予算: {args.budget_mb:.0f} MB")
    for zoom, count, size in plan:
        print(f"  zoom {zoom:2d}: {count:6d} タイル, 約 {size / 1024 / 1024:.1f} MB")
    print(f"合計見積り: 約 {total_bytes / 1024 / 1024:.1f} MB, "
          f"タイル数 {sum(c for _, c, _ in plan)}")
    est_seconds = sum(c for _, c, _ in plan) * args.rate_sec
    print(f"想定ダウンロード時間: 約 {est_seconds / 60:.1f} 分（レート制限 {args.rate_sec}秒/枚）")

    if args.dry_run:
        return

    dest_dir = config.TILE_PATH
    os.makedirs(dest_dir, exist_ok=True)
    downloaded = 0
    skipped = 0
    failed = 0
    for zoom, count, _ in plan:
        for z, x, y in tiles_for_zoom(bbox, zoom):
            try:
                if download_tile(z, x, y, dest_dir):
                    downloaded += 1
                    time.sleep(args.rate_sec)
                else:
                    skipped += 1
            except Exception as e:
                failed += 1
                print(f"失敗: z={z} x={x} y={y}: {e}", file=sys.stderr)
            if (downloaded + skipped) % 200 == 0:
                print(f"進捗: 新規{downloaded} / 既存スキップ{skipped} / 失敗{failed}")

    print(f"完了: 新規{downloaded}枚, 既存スキップ{skipped}枚, 失敗{failed}枚")


if __name__ == "__main__":
    main()
