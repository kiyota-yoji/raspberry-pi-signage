import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from signage import bootimport, config, sync
from signage.player import Player

EXAMPLE = Path(__file__).parent.parent / "config" / "signage.toml.example"


class BootImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.boot = t / "boot"
        self.boot.mkdir()
        self.etc = t / "etc" / "signage.toml"
        # 引数と標準入力を記録する偽の setup-wifi.sh
        self.log = t / "wifi.log"
        script = t / "setup-wifi.sh"
        script.write_text(f'#!/bin/sh\necho "$@" > "{self.log}"\ncat >> "{self.log}"\necho 接続しました\n')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        self.env = mock.patch.dict(os.environ, {"SIGNAGE_BOOT_DIR": str(self.boot)})
        self.env.start()
        self.script = mock.patch.object(bootimport, "WIFI_SCRIPT", script)
        self.script.start()

    def tearDown(self):
        self.script.stop()
        self.env.stop()
        self.tmp.cleanup()

    def result(self):
        return (self.boot / "signage-result.txt").read_text()

    def test_nothing_to_do(self):
        self.assertEqual(bootimport.run(self.etc), 0)
        self.assertFalse((self.boot / "signage-result.txt").exists())

    def test_config_applied_with_bom_and_crlf(self):
        text = EXAMPLE.read_text().replace("\n", "\r\n")
        (self.boot / "signage.toml").write_bytes(b"\xef\xbb\xbf" + text.encode())
        self.etc.parent.mkdir()
        self.etc.write_text("old")
        self.assertEqual(bootimport.run(self.etc), 0)
        self.assertEqual(config.load(self.etc)["playlist"]["url"],
                         "https://math.kiyota-yoji.net/istudio-signage/playlist.json")
        self.assertEqual((self.etc.parent / "signage.toml.bak").read_text(), "old")
        self.assertTrue((self.boot / "signage.toml.applied").exists())
        self.assertFalse((self.boot / "signage.toml").exists())
        self.assertIn("[OK]", self.result())

    def test_invalid_config_not_applied(self):
        (self.boot / "signage.toml").write_text('[schedule]\nmon = ["8時から"]\n[playlist]\nurl="https://x"\n')
        self.etc.parent.mkdir()
        self.etc.write_text("old")
        self.assertEqual(bootimport.run(self.etc), 1)
        self.assertEqual(self.etc.read_text(), "old")
        self.assertTrue((self.boot / "signage.toml.error").exists())
        self.assertIn("[NG]", self.result())

    def test_wifi_enterprise(self):
        (self.boot / "wifi.txt").write_text(
            "# comment\nssid = olive\neap = peap\nidentity = sig@example.ac.jp\npassword = \"p@ss word\"\n")
        self.assertEqual(bootimport.run(self.etc), 0)
        args, stdin = self.log.read_text().split("\n", 1)
        self.assertIn("--ssid olive", args)
        self.assertIn("--eap peap", args)
        self.assertIn("--name campus-wifi", args)
        self.assertNotIn("p@ss", args)          # パスワードは引数に載せない
        self.assertEqual(stdin, "p@ss word\n")
        self.assertFalse((self.boot / "wifi.txt").exists())
        self.assertIn("olive", self.result())

    def test_wifi_uses_connection_name_from_config(self):
        self.etc.parent.mkdir()
        self.etc.write_text(EXAMPLE.read_text().replace('connection = "campus-wifi"', 'connection = "olive"'))
        (self.boot / "wifi.txt").write_text("ssid=olive\npassword=x\n")
        bootimport.run(self.etc)
        self.assertIn("--name olive", self.log.read_text())

    def test_invalid_wifi_deleted(self):
        (self.boot / "wifi.txt").write_text("ssid = olive\neap = peap\npassword = secret\n")
        self.assertEqual(bootimport.run(self.etc), 1)
        self.assertFalse((self.boot / "wifi.txt").exists())
        self.assertFalse(self.log.exists())
        self.assertIn("identity", self.result())
        self.assertNotIn("secret", self.result())

    def test_example_wifi_parses(self):
        w = bootimport.parse_wifi((Path(__file__).parent.parent / "examples" / "wifi.txt").read_text())
        self.assertEqual(w["ssid"], "olive")


class FakeMpv:
    def __init__(self):
        self.stopped = 0
        self.started = 0
        self.proc = None

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def alive(self):
        return False


class FakeDisplay:
    def __init__(self):
        self.calls = []

    def on(self):
        self.calls.append("on")

    def off(self):
        self.calls.append("off")


class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        (t / "boot").mkdir()
        self.boot = t / "boot"
        self.env = mock.patch.dict(os.environ, {"SIGNAGE_BOOT_DIR": str(self.boot)})
        self.env.start()
        cfg = config._merge(config.DEFAULTS, {
            "storage": {"dir": str(t / "data")}, "display": {"methods": []},
            "schedule": {d: ["00:00-24:00"] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}})
        self.p = Player(cfg)
        self.p.mpv = FakeMpv()
        self.p.display = FakeDisplay()
        self.p.turn_on = lambda: setattr(self.p, "state", "on")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def tick(self):
        # Player.run() の 1 回分を、イベント待ち無しで実行する
        self.p.running = True
        orig = self.p.mpv
        orig.events = lambda timeout: setattr(self.p, "running", False) or []
        with mock.patch("signage.player.shutil.which", return_value="/usr/bin/mpv"), \
             mock.patch("signage.player.signal.signal"):
            self.p.run()

    def test_maintenance_file_stops_playback(self):
        (self.boot / "signage-maintenance.txt").write_text("")
        self.tick()
        self.assertEqual(self.p.state, "maintenance")
        self.assertIn("on", self.p.display.calls)   # 画面（ログインプロンプト）を見せる
        status = sync.read_json(self.p.paths.status)
        self.assertTrue(status["maintenance_file"].endswith("signage-maintenance.txt"))
        # ファイルを消すと通常運用に戻る
        (self.boot / "signage-maintenance.txt").unlink()
        self.tick()
        self.assertEqual(self.p.state, "on")
        self.assertEqual(self.p.mpv.started, 1)


if __name__ == "__main__":
    unittest.main()
