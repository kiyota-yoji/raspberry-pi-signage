"""mpv を制御して動画を再生し続けるデーモン。

- mpv は JSON IPC で制御する。mpv の再生リストには常に「再生中」と「次」の 2 本だけを置き、
  動画が切り替わるたびに次の 1 本を補充する（切り替えは途切れず、リスト更新も次の動画から反映）。
- マニフェスト (sync が書く) の更新を監視し、変わったら次の動画から新しいリストで再生する。
- スケジュール外の時間帯は mpv を終了し、ディスプレイを消灯する。
"""

from __future__ import annotations

import json
import logging
import os
import random
import select
import shutil
import signal
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import schedule as sched
from . import config
from .config import Paths
from .display import Display
from .sync import read_json, write_json_atomic

log = logging.getLogger("signage.player")

IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
DEFAULT_IMAGE_SEC = 10


# ---------------------------------------------------------------- 再生順


class Sequencer:
    """マニフェストから「次に再生する項目」を決める。"""

    def __init__(self, manifest: dict | None, rng: random.Random | None = None):
        self.rng = rng or random.Random()
        self.manifest = manifest or {}
        self.queue: list[dict] = []
        self.last: dict | None = None

    def _active(self, now: datetime) -> list[dict]:
        return [
            it for it in self.manifest.get("items", [])
            if sched.in_window(now, it.get("valid_from"), it.get("valid_until"))
            and os.path.exists(it["path"])
        ]

    def _build_round(self, now: datetime, after_path: str | None) -> list[dict]:
        items = self._active(now)
        if not items:
            return []
        if self.manifest.get("mode") == "shuffle":
            items = items[:]
            self.rng.shuffle(items)
            # 同じ動画が連続しないように
            if len(items) > 1 and after_path and items[0]["path"] == after_path:
                items.append(items.pop(0))
        elif after_path:
            # 順番再生: 直前に再生していた動画の次から続ける
            paths = [it["path"] for it in items]
            if after_path in paths:
                k = paths.index(after_path) + 1
                items = items[k:] + items[:k]
        out = []
        for it in items:
            out.extend([it] * it.get("repeat", 1))
        return out

    def reset(self, manifest: dict | None) -> None:
        self.manifest = manifest or {}
        self.queue = []

    def next(self, now: datetime | None = None) -> dict | None:
        now = now or datetime.now()
        # 掲出期間切れ・削除済みの項目は飛ばす
        while self.queue:
            it = self.queue.pop(0)
            if sched.in_window(now, it.get("valid_from"), it.get("valid_until")) and os.path.exists(it["path"]):
                self.last = it
                return it
        after = self.last["path"] if self.last else None
        self.queue = self._build_round(now, after)
        if not self.queue:
            return None
        self.last = self.queue.pop(0)
        return self.last


# ---------------------------------------------------------------- mpv


class Mpv:
    def __init__(self, cfg: dict, socket_path: Path):
        self.cfg = cfg
        self.socket_path = socket_path
        self.proc: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self.buf = b""

    def start(self) -> None:
        self.socket_path.unlink(missing_ok=True)
        p = self.cfg["player"]
        args = [
            "mpv",
            "--idle=yes",
            "--force-window=yes",  # 動画の切り替え時・待機中もコンソールを見せず黒画面にする
            f"--input-ipc-server={self.socket_path}",
            "--fullscreen",
            "--vo=gpu",
            "--gpu-context=drm",
            "--hwdec=auto-safe",
            "--no-osc",
            "--osd-level=0",
            "--no-input-default-bindings",
            "--input-terminal=no",
            "--cursor-autohide=always",
            "--keep-open=no",
            "--loop-playlist=no",
            "--prefetch-playlist=yes",
            f"--image-display-duration={DEFAULT_IMAGE_SEC}",
            "--msg-level=all=warn",
            f"--mute={'yes' if p.get('mute', True) else 'no'}",
            *p.get("mpv_extra_args", []),
        ]
        log.info("mpv を起動します")
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"mpv が終了しました (code {self.proc.returncode})")
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(str(self.socket_path))
                s.setblocking(False)
                self.sock = s
                self.buf = b""
                self.command("observe_property", 1, "idle-active")
                return
            except (FileNotFoundError, ConnectionRefusedError):
                s.close()
                time.sleep(0.2)
        self.stop()
        raise RuntimeError("mpv の IPC ソケットに接続できません")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def send(self, payload: dict) -> None:
        if not self.sock:
            return
        data = (json.dumps(payload) + "\n").encode()
        try:
            self.sock.setblocking(True)
            self.sock.sendall(data)
        except OSError as e:
            log.warning("mpv への送信に失敗: %s", e)
        finally:
            self.sock.setblocking(False)

    def command(self, *args) -> None:
        self.send({"command": list(args)})

    def loadfile(self, item: dict, flags: str) -> None:
        options = {}
        if Path(item["path"]).suffix.lower() in IMAGE_EXTS:
            options["image-display-duration"] = str(item.get("duration") or DEFAULT_IMAGE_SEC)
        # 名前付き引数で送る（mpv のバージョンによる位置引数の違いを避ける）
        cmd = {"name": "loadfile", "url": item["path"], "flags": flags}
        if options:
            cmd["options"] = options
        self.send({"command": cmd})

    def events(self, timeout: float) -> list[dict]:
        if not self.sock:
            time.sleep(timeout)
            return []
        r, _, _ = select.select([self.sock], [], [], timeout)
        if not r:
            return []
        try:
            chunk = self.sock.recv(65536)
        except BlockingIOError:
            return []
        except OSError:
            chunk = b""
        if not chunk:  # mpv 側が閉じた
            self.sock.close()
            self.sock = None
            return []
        self.buf += chunk
        out = []
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "event" in msg:
                out.append(msg)
            elif msg.get("error") not in (None, "success"):
                log.warning("mpv コマンドエラー: %s", msg.get("error"))
        return out

    def stop(self) -> None:
        if self.alive():
            self.command("quit")
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self.sock:
            self.sock.close()
            self.sock = None
        self.proc = None
        log.info("mpv を停止しました")


# ---------------------------------------------------------------- 本体


class Player:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.paths = Paths(cfg)
        self.paths.ensure()
        self.display = Display(cfg)
        self.mpv = Mpv(cfg, self.paths.mpv_socket)
        self.manifest_mtime = None
        self.manifest = None
        self.seq = Sequencer(None)
        self.current: dict | None = None  # 再生中
        self.upcoming: dict | None = None  # mpv の再生リストに積んだ次の 1 本
        self.idle = False
        self.showing_fallback = False
        self.empty_logged = False
        self.state: str | None = None  # "on" / "off"
        self.last_off_assert = 0.0
        self.restart_after = 0.0
        self.crashes = 0
        self.running = True
        sched.validate(cfg["schedule"])

    # -- 設定・状態 --
    def effective_schedule(self) -> dict:
        local = self.cfg["schedule"]
        remote = (self.manifest or {}).get("schedule")
        if remote and local.get("allow_remote", True):
            return remote
        return local

    def override(self) -> str | None:
        try:
            v = self.paths.override.read_text().strip().lower()
        except FileNotFoundError:
            return None
        return v if v in ("on", "off") else None

    def desired_on(self) -> bool:
        ov = self.override()
        if ov:
            return ov == "on"
        return sched.is_on(datetime.now(), self.effective_schedule())

    def write_status(self) -> None:
        write_json_atomic(self.paths.status, {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "state": self.state,
            "maintenance_file": str(mf) if (mf := config.maintenance_file()) else None,
            "override": self.override(),
            "playing": (self.current or {}).get("title") or (self.current or {}).get("path"),
            "mpv_running": self.mpv.alive(),
            "items": len((self.manifest or {}).get("items", [])),
            "manifest_generated_at": (self.manifest or {}).get("generated_at"),
        })

    def check_manifest(self) -> bool:
        """マニフェストが更新されていれば読み直す。"""
        try:
            mtime = self.paths.manifest.stat().st_mtime
        except FileNotFoundError:
            mtime = None
        if mtime == self.manifest_mtime:
            return False
        self.manifest_mtime = mtime
        self.manifest = read_json(self.paths.manifest, None)
        last = self.seq.last
        self.seq.reset(self.manifest)
        self.seq.last = last
        log.info("マニフェストを読み込みました（%d 本）", len((self.manifest or {}).get("items", [])))
        return True

    # -- 再生制御 --
    def play_fresh(self) -> None:
        """再生リストを最初から組み直して再生する。"""
        item = self.seq.next()
        if item:
            self.showing_fallback = False
            self.empty_logged = False
            self.upcoming = item
            self.mpv.loadfile(item, "replace")
            return
        if not self.empty_logged:
            log.info("再生できる動画がありません（同期・掲出開始を待っています）")
            self.empty_logged = True
        fb = self.cfg["player"].get("fallback_image")
        if fb and os.path.exists(fb) and not self.showing_fallback:
            self.showing_fallback = True
            self.current = None
            self.mpv.send({"command": {"name": "loadfile", "url": fb, "flags": "replace",
                                       "options": {"image-display-duration": "inf"}}})

    def queue_next(self) -> None:
        """mpv の再生リストを「再生中 + 次の 1 本」にする。"""
        self.mpv.command("playlist-clear")  # 再生中以外を消す
        self.upcoming = self.seq.next()
        if self.upcoming:
            # append-play: 待機中なら再生を始め、再生中なら末尾に追加するだけ
            self.mpv.loadfile(self.upcoming, "append-play")

    def on_event(self, ev: dict) -> None:
        name = ev.get("event")
        if name == "start-file":
            if not self.showing_fallback:
                self.current = self.upcoming
                self.queue_next()
        elif name == "end-file" and ev.get("reason") == "error":
            log.warning("再生エラー（ファイル破損・非対応形式の可能性）: %s", ev.get("file_error"))
        elif name == "property-change" and ev.get("name") == "idle-active":
            self.idle = bool(ev.get("data"))
            if self.idle:
                self.current = None
                self.play_fresh()

    def turn_on(self) -> None:
        self.display.on()
        time.sleep(3)  # テレビの起動と HDMI の再認識を待つ
        self.start_mpv()
        self.state = "on"

    def turn_off(self) -> None:
        self.mpv.stop()
        self.display.off()
        self.last_off_assert = time.monotonic()
        self.state = "off"

    def start_mpv(self) -> None:
        try:
            self.showing_fallback = False
            self.idle = False
            self.mpv.start()
        except (RuntimeError, OSError) as e:
            self.crashes += 1
            delay = min(300, 5 * 2 ** min(self.crashes, 6))
            log.error("%s（%d 秒後に再試行）", e, delay)
            self.restart_after = time.monotonic() + delay

    def stop(self, *_):
        self.running = False

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        if not shutil.which("mpv"):
            log.error("mpv がインストールされていません")
            return 1
        reassert = self.cfg["display"].get("reassert_off_min", 30) * 60
        next_check = 0.0
        try:
            while self.running:
                now = time.monotonic()
                if now >= next_check:
                    next_check = now + 5
                    changed = self.check_manifest()
                    want = self.desired_on()
                    mf = config.maintenance_file()
                    if mf:
                        if self.state != "maintenance":
                            # 再生を止め、画面にログインプロンプトを出す（キーボードで作業できる）
                            log.info("%s があるためメンテナンスモードにします（再生しません）", mf)
                            self.mpv.stop()
                            self.display.on()
                            self.state = "maintenance"
                    elif want and self.state != "on":
                        log.info("再生時間帯に入りました")
                        self.turn_on()
                    elif not want and self.state != "off":
                        log.info("再生時間外になりました")
                        self.turn_off()
                    elif not want and reassert and now - self.last_off_assert >= reassert:
                        self.display.off()
                        self.last_off_assert = now
                    elif want and self.mpv.alive():
                        if self.idle or self.showing_fallback:
                            # 待機中: 新しい動画の到着や掲出開始時刻を待って再生を始める
                            self.play_fresh()
                        elif changed:
                            self.queue_next()  # 次の動画から新しいリストを反映
                    # mpv が落ちていたら再起動
                    if self.state == "on" and not self.mpv.alive() and now >= self.restart_after:
                        if self.mpv.proc is not None:
                            log.warning("mpv が終了していたため再起動します")
                            self.mpv.proc = None
                        self.start_mpv()
                    elif self.state == "on" and self.mpv.alive():
                        self.crashes = 0
                    self.write_status()
                for ev in self.mpv.events(1.0):
                    self.on_event(ev)
        finally:
            self.mpv.stop()
        return 0


def run(cfg: dict) -> int:
    return Player(cfg).run()
