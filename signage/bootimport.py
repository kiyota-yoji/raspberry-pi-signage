"""起動時に SD カードの boot 領域から設定を取り込む（root で、再生開始前に実行）。

boot 領域（/boot/firmware）は FAT なので、SD カードを Mac / Windows に挿して編集できる。

  signage.toml  → 検証して /etc/signage/signage.toml に反映し、signage.toml.applied に改名
                  （不正なら反映せず signage.toml.error に改名）
  wifi.txt      → Wi-Fi 接続を登録し、パスワードを含むため削除
  結果は signage-result.txt に書き出す（Mac で開いて確認できる）
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tomllib
from datetime import datetime
from pathlib import Path

from . import config, schedule
from .display import Display

log = logging.getLogger("signage.bootimport")

WIFI_SCRIPT = Path(os.environ.get("SIGNAGE_WIFI_SCRIPT", "/opt/signage/scripts/setup-wifi.sh"))
WIFI_NAMES = ("wifi.txt", "wifi.txt.txt")

# wifi.txt のキー → setup-wifi.sh の引数
WIFI_KEYS = {
    "ssid": "--ssid",
    "eap": "--eap",
    "phase2": "--phase2",
    "identity": "--identity",
    "anonymous_identity": "--anonymous-identity",
    "ca_cert": "--ca-cert",
    "domain_suffix_match": "--domain-suffix-match",
    "country": "--country",
    "priority": "--priority",
}


def _read_text(path: Path) -> str:
    # Windows のメモ帳が付ける BOM と CRLF を許容
    return path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")


# ---------------------------------------------------------------- 設定ファイル


def import_config(src: Path, dest: Path) -> list[str]:
    try:
        text = _read_text(src)
        cfg = config._merge(config.DEFAULTS, tomllib.loads(text))
        schedule.validate(cfg["schedule"])
        Display(cfg)
        if not cfg["playlist"]["url"]:
            raise ValueError("playlist.url が空です")
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError, TypeError, AttributeError) as e:
        src.replace(src.with_name("signage.toml.error"))
        log.error("signage.toml が不正なため反映しません: %s", e)
        return [f"[NG] signage.toml が不正なため反映しませんでした（signage.toml.error に改名）: {e}",
               "     修正して signage.toml に名前を戻し、再起動してください。"]

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.copy2(dest, dest.with_name(dest.name + ".bak"))
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o640)
    try:
        shutil.chown(tmp, "root", "signage")
    except (LookupError, PermissionError):
        pass
    os.replace(tmp, dest)
    src.replace(src.with_name("signage.toml.applied"))
    log.info("signage.toml を %s に反映しました", dest)
    return [f"[OK] signage.toml を反映しました（前の設定は {dest.name}.bak。boot 側は signage.toml.applied に改名）"]


# ---------------------------------------------------------------- Wi-Fi


def parse_wifi(text: str) -> dict:
    values = {}
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{n} 行目に = がありません: {line!r}")
        key, value = (s.strip() for s in line.split("=", 1))
        key = key.lower().replace("-", "_")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key not in WIFI_KEYS and key not in ("password", "hidden", "name"):
            raise ValueError(f"{n} 行目: 不明な項目 {key!r}")
        values[key] = value
    if not values.get("ssid"):
        raise ValueError("ssid がありません")
    if values.get("eap") and not values.get("identity"):
        raise ValueError("eap を指定した場合は identity が必要です")
    if "password" not in values:
        raise ValueError("password がありません")
    return values


def import_wifi(src: Path, default_name: str) -> list[str]:
    try:
        try:
            w = parse_wifi(_read_text(src))
        except (UnicodeDecodeError, ValueError) as e:
            log.error("wifi.txt が不正です: %s", e)
            return [f"[NG] wifi.txt が不正なため登録しませんでした: {e}",
                    "     （パスワードを含むため wifi.txt は削除しました。作り直してください）"]

        name = w.get("name") or default_name or "campus-wifi"
        args = [str(WIFI_SCRIPT), "--name", name]
        for key, opt in WIFI_KEYS.items():
            if w.get(key):
                args += [opt, w[key]]
        if w.get("hidden", "").lower() in ("yes", "true", "1"):
            args.append("--hidden")
        try:
            # パスワードはコマンドライン引数に載せず標準入力で渡す
            r = subprocess.run(args, input=w["password"] + "\n", capture_output=True,
                               text=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as e:
            return [f"[NG] Wi-Fi の登録に失敗しました: {e}"]
        out = (r.stdout + r.stderr).strip()
        log.info("setup-wifi.sh:\n%s", out)
        head = "[OK]" if r.returncode == 0 else "[NG]"
        return [f"{head} Wi-Fi「{w['ssid']}」を接続名 {name} で登録しました"
                if r.returncode == 0 else f"{head} Wi-Fi の登録に失敗しました（終了コード {r.returncode}）",
                *("     " + line for line in out.splitlines())]
    finally:
        src.unlink(missing_ok=True)


# ---------------------------------------------------------------- 本体


def _first(boot: Path, names) -> Path | None:
    for n in names:
        if (boot / n).is_file():
            return boot / n
    return None


def run(config_path: Path | None = None) -> int:
    boot = config.boot_dir()
    dest = Path(config_path) if config_path else config.DEFAULT_CONFIG_PATH
    lines: list[str] = []

    src = boot / "signage.toml"
    if src.is_file():
        lines += import_config(src, dest)

    wifi = _first(boot, WIFI_NAMES)
    if wifi:
        try:
            name = config.load(dest)["network"]["connection"]
        except (OSError, tomllib.TOMLDecodeError):
            name = ""
        lines += import_wifi(wifi, name)

    mf = config.maintenance_file()
    if mf:
        lines.append(f"[INFO] {mf.name} があるため、メンテナンスモードで起動しました（再生しません）")

    if not lines:
        return 0
    report = [f"signage 起動時の設定取り込み {datetime.now():%Y-%m-%d %H:%M:%S}", "", *lines, ""]
    (boot / "signage-result.txt").write_text("\r\n".join(report), encoding="utf-8")
    os.sync()  # SD カードをすぐ抜かれても残るように
    return 1 if any(line.startswith("[NG]") for line in lines) else 0
