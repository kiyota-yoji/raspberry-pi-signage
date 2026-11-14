"""設定ファイル (TOML) の読み込み。"""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(os.environ.get("SIGNAGE_CONFIG", "/etc/signage/signage.toml"))


def boot_dir() -> Path:
    """SD カードの boot 領域（FAT。Mac / Windows からも読み書きできる）。"""
    env = os.environ.get("SIGNAGE_BOOT_DIR")
    if env:
        return Path(env)
    return Path("/boot/firmware") if Path("/boot/firmware").is_dir() else Path("/boot")


# boot 領域に置くと再生を止めるファイル（テキストエディタで .txt が付いても可）
MAINTENANCE_NAMES = ("signage-maintenance", "signage-maintenance.txt")


def maintenance_file() -> Path | None:
    for name in MAINTENANCE_NAMES:
        p = boot_dir() / name
        if p.exists():
            return p
    return None

DEFAULTS: dict = {
    "playlist": {"url": "", "timeout_sec": 30, "username": "", "password": ""},
    "storage": {"dir": "/var/lib/signage", "delete_grace_min": 30},
    "player": {"mute": True, "mpv_extra_args": [], "fallback_image": ""},
    "display": {
        "methods": ["cec", "fb_blank"],
        "cec_device": "/dev/cec0",
        "reassert_off_min": 30,
    },
    "schedule": {
        "mon": [], "tue": [], "wed": [], "thu": [], "fri": [], "sat": [], "sun": [],
        "off_dates": [],
        "allow_remote": True,
    },
    "network": {
        "connection": "",
        "check_url": "",
        "reconnect_after": 2,
        "restart_nm_after": 4,
        "reboot_after": 0,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path: Path | str | None = None) -> dict:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(path, "rb") as f:
        return _merge(DEFAULTS, tomllib.load(f))


class Paths:
    """保存先ディレクトリ配下のファイル配置。"""

    def __init__(self, cfg: dict):
        self.root = Path(cfg["storage"]["dir"])
        self.media = self.root / "media"
        self.manifest = self.root / "manifest.json"  # sync が書き、player が読む
        self.remote_cache = self.root / "playlist.remote.json"  # 最後に取得できた JSON
        self.pending_delete = self.root / "pending_delete.json"
        self.override = self.root / "override"  # "on" / "off" / 無し(=自動)
        self.status = self.root / "status.json"  # player の現在状態
        self.netwatch_state = self.root / "netwatch.json"
        self.lock = self.root / "sync.lock"
        self.mpv_socket = self.root / "mpv.sock"

    def ensure(self) -> None:
        self.media.mkdir(parents=True, exist_ok=True)
