"""ディスプレイの点灯・消灯。"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("signage.display")

FB_BLANK = Path("/sys/class/graphics/fb0/blank")


def _run(cmd: list[str]) -> bool:
    if not shutil.which(cmd[0]):
        log.warning("%s が見つかりません", cmd[0])
        return False
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        log.warning("タイムアウト: %s", " ".join(cmd))
        return False
    if r.returncode != 0:
        log.warning("失敗 (%d): %s %s", r.returncode, " ".join(cmd), r.stderr.strip())
        return False
    return True


class Display:
    def __init__(self, cfg: dict):
        d = cfg["display"]
        self.methods = list(d.get("methods", []))
        self.cec_device = d.get("cec_device", "/dev/cec0")
        unknown = set(self.methods) - {"cec", "fb_blank", "vcgencmd"}
        if unknown:
            raise ValueError(f"display.methods に不明な値があります: {sorted(unknown)}")

    # -- CEC (v4l-utils の cec-ctl) --
    def _cec(self, *args: str) -> bool:
        # 自身を「再生機器」として登録してから命令を送る（毎回行っても問題ない）
        _run(["cec-ctl", "-d", self.cec_device, "--playback"])
        return _run(["cec-ctl", "-d", self.cec_device, "--to", "0", *args])

    def _fb_blank(self, blank: bool) -> bool:
        try:
            # 4 = FB_BLANK_POWERDOWN（HDMI 出力を DPMS オフ）, 0 = 復帰
            FB_BLANK.write_text("4" if blank else "0")
            return True
        except OSError as e:
            log.warning("%s に書き込めません: %s", FB_BLANK, e)
            return False

    def on(self) -> None:
        log.info("ディスプレイを点灯します（%s）", ", ".join(self.methods) or "なし")
        for m in self.methods:
            if m == "cec":
                self._cec("--image-view-on")
            elif m == "fb_blank":
                self._fb_blank(False)
            elif m == "vcgencmd":
                _run(["vcgencmd", "display_power", "1"])

    def off(self) -> None:
        log.info("ディスプレイを消灯します（%s）", ", ".join(self.methods) or "なし")
        for m in self.methods:
            if m == "cec":
                self._cec("--standby")
            elif m == "fb_blank":
                self._fb_blank(True)
            elif m == "vcgencmd":
                _run(["vcgencmd", "display_power", "0"])
