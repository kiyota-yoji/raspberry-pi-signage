"""Wi-Fi の疎通監視。つながらない状態が続いたら段階的に復旧を試みる（root で実行）。

  連続失敗 reconnect_after 回 → nmcli connection up <接続名>
  連続失敗 restart_nm_after 回 → NetworkManager を再起動
  連続失敗 reboot_after 回    → OS 再起動（0 で無効）
"""

from __future__ import annotations

import logging
import subprocess
import urllib.error
import urllib.request

from .config import Paths
from .sync import USER_AGENT, read_json, write_json_atomic

log = logging.getLogger("signage.netwatch")


def online(url: str, timeout: float = 15) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        # HTTP の応答が返ってくればネットワーク自体は生きている
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _sh(cmd: list[str]) -> None:
    log.warning("実行: %s", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        log.error("失敗 (%d): %s", r.returncode, r.stderr.strip())


def run(cfg: dict) -> int:
    paths = Paths(cfg)
    paths.ensure()
    net = cfg["network"]
    url = net.get("check_url") or cfg["playlist"]["url"]
    if not url:
        log.error("network.check_url と playlist.url が両方空です")
        return 1

    state = read_json(paths.netwatch_state, {}) or {}
    fails = state.get("fails", 0)

    if online(url):
        if fails:
            log.info("ネットワークが復旧しました（%d 回連続失敗の後）", fails)
        write_json_atomic(paths.netwatch_state, {"fails": 0})
        return 0

    fails += 1
    log.warning("疎通確認に失敗しました（%d 回連続）: %s", fails, url)
    write_json_atomic(paths.netwatch_state, {"fails": fails})

    conn = net.get("connection")
    reboot_after = net.get("reboot_after", 0)
    if reboot_after and fails >= reboot_after:
        write_json_atomic(paths.netwatch_state, {"fails": 0})
        _sh(["systemctl", "reboot"])
    elif fails >= net.get("restart_nm_after", 4) and fails % 2 == 0:
        _sh(["systemctl", "restart", "NetworkManager"])
    elif fails >= net.get("reconnect_after", 2) and conn:
        _sh(["nmcli", "connection", "up", conn])
    return 1
