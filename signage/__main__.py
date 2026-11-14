"""コマンドライン: python3 -m signage <command>

  sync              再生リスト JSON を取得して動画をダウンロード
  player            再生デーモン（systemd から起動）
  netwatch          Wi-Fi 疎通監視（systemd タイマーから起動）
  status            現在の状態を表示
  override on|off|auto
                    スケジュールを無視して強制点灯 / 強制消灯 / 自動に戻す
  check             設定ファイルと再生リスト JSON を検証（ダウンロードはしない）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime

from . import config, schedule
from .config import Paths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="signage", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", help="設定ファイル（既定: /etc/signage/signage.toml）")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("sync", "player", "netwatch", "status", "check"):
        sub.add_parser(name)
    ov = sub.add_parser("override")
    ov.add_argument("mode", choices=["on", "off", "auto"])
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    cfg = config.load(args.config)
    paths = Paths(cfg)

    if args.cmd == "sync":
        from . import sync
        return sync.run(cfg)
    if args.cmd == "player":
        from . import player
        return player.run(cfg)
    if args.cmd == "netwatch":
        from . import netwatch
        return netwatch.run(cfg)
    if args.cmd == "override":
        if args.mode == "auto":
            paths.override.unlink(missing_ok=True)
        else:
            paths.override.write_text(args.mode + "\n")
        print(f"override = {args.mode}（5 秒以内に反映されます）")
        return 0
    if args.cmd == "status":
        from .sync import read_json
        st = read_json(paths.status, None)
        print(json.dumps(st, ensure_ascii=False, indent=2) if st else "player が動いていません")
        return 0
    if args.cmd == "check":
        from . import sync
        from .display import Display
        schedule.validate(cfg["schedule"])
        Display(cfg)
        print("設定ファイル: OK")
        pl, _ = sync.fetch_playlist(cfg, sync.Fetcher(cfg))
        print(f"再生リスト: OK（mode={pl['mode']}, {len(pl['items'])} 本）")
        for it in pl["items"]:
            window = f"  [{it['valid_from'] or ''} 〜 {it['valid_until'] or ''}]" if (
                it["valid_from"] or it["valid_until"]) else ""
            print(f"  - {it['title'] or it['url']}  x{it['repeat']}{window}")
        sch = pl["schedule"] if (pl["schedule"] and cfg["schedule"]["allow_remote"]) else cfg["schedule"]
        src = "再生リスト JSON" if sch is pl["schedule"] else "設定ファイル"
        print(f"スケジュール（{src}）: 現在は {'ON' if schedule.is_on(datetime.now(), sch) else 'OFF'}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
