import hashlib
import json
import os
import random
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from signage import config, schedule, sync
from signage.config import Paths
from signage.player import Player, Sequencer

SCHED = {
    "mon": ["08:00-20:00"], "tue": ["08:00-12:00", "13:00-18:00"], "wed": [],
    "thu": [], "fri": ["22:00-02:00"], "sat": [], "sun": ["09:00-24:00"],
    "off_dates": ["2026-11-17"],  # 火曜
}


class ScheduleTest(unittest.TestCase):
    def at(self, s):
        return schedule.is_on(datetime.fromisoformat(s), SCHED)

    def test_ranges(self):
        self.assertFalse(self.at("2026-11-16T07:59"))  # 月
        self.assertTrue(self.at("2026-11-16T08:00"))
        self.assertFalse(self.at("2026-11-16T20:00"))
        self.assertTrue(self.at("2026-11-24T12:30") is False)  # 火の昼休み
        self.assertTrue(self.at("2026-11-24T13:30"))

    def test_off_dates(self):
        self.assertFalse(self.at("2026-11-17T09:00"))

    def test_overnight(self):
        self.assertFalse(self.at("2026-11-20T21:59"))  # 金
        self.assertTrue(self.at("2026-11-20T23:00"))
        self.assertTrue(self.at("2026-11-21T01:59"))  # 土の未明
        self.assertFalse(self.at("2026-11-21T02:00"))

    def test_until_midnight(self):
        self.assertTrue(self.at("2026-11-22T23:59:30"))  # 日

    def test_validate(self):
        with self.assertRaises(ValueError):
            schedule.validate({"mon": ["8時-9時"]})

    def test_window(self):
        now = datetime(2026, 12, 20, 23, 0)
        self.assertTrue(schedule.in_window(now, None, "2026-12-20"))
        self.assertFalse(schedule.in_window(now, None, "2026-12-20T22:00"))
        self.assertFalse(schedule.in_window(now, "2026-12-21", None))


def items(*names, **kw):
    return [{"path": n, "repeat": kw.get(n, 1)} for n in names]


class SequencerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        for n in "abc":
            (self.d / n).write_text(n)

    def tearDown(self):
        self.tmp.cleanup()

    def p(self, n):
        return str(self.d / n)

    def test_sequential_repeat(self):
        s = Sequencer({"mode": "sequential", "items": [
            {"path": self.p("a"), "repeat": 2}, {"path": self.p("b")}]})
        got = [Path(s.next()["path"]).name for _ in range(6)]
        self.assertEqual(got, ["a", "a", "b", "a", "a", "b"])

    def test_continue_after_reset(self):
        m = {"mode": "sequential", "items": [{"path": self.p(n)} for n in "abc"]}
        s = Sequencer(m)
        self.assertEqual(Path(s.next()["path"]).name, "a")
        self.assertEqual(Path(s.next()["path"]).name, "b")
        last = s.last
        s.reset(m)
        s.last = last
        self.assertEqual(Path(s.next()["path"]).name, "c")

    def test_missing_and_expired_skipped(self):
        s = Sequencer({"mode": "sequential", "items": [
            {"path": self.p("a"), "valid_until": "2000-01-01"},
            {"path": self.p("zzz")},
            {"path": self.p("b")}]})
        self.assertEqual(Path(s.next()["path"]).name, "b")
        self.assertEqual(Path(s.next()["path"]).name, "b")

    def test_shuffle_no_immediate_repeat(self):
        s = Sequencer({"mode": "shuffle", "items": [{"path": self.p(n)} for n in "abc"]},
                      rng=random.Random(1))
        got = [s.next()["path"] for _ in range(300)]
        self.assertTrue(all(x != y for x, y in zip(got, got[1:])))
        self.assertEqual(set(got), {self.p(n) for n in "abc"})

    def test_empty(self):
        self.assertIsNone(Sequencer({"items": []}).next())
        self.assertIsNone(Sequencer(None).next())


# ---------------------------------------------------------------- HTTP サーバー


class Handler(BaseHTTPRequestHandler):
    files: dict = {}
    hits: list = []
    fail_after: dict = {}  # path -> 途中で切断するバイト数（1 回だけ）

    def log_message(self, *a):
        pass

    def do_GET(self):
        Handler.hits.append((self.path, dict(self.headers)))
        body = Handler.files.get(self.path)
        if body is None:
            self.send_error(404)
            return
        etag = '"%s"' % hashlib.md5(body).hexdigest()
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.end_headers()
            return
        start = 0
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(body):
                self.send_response(416)
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        else:
            self.send_response(200)
        chunk = body[start:]
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("ETag", etag)
        self.end_headers()
        cut = Handler.fail_after.pop(self.path, None)
        if cut is not None:
            self.wfile.write(chunk[:cut])
            self.wfile.flush()
            self.connection.shutdown(2)
            return
        self.wfile.write(chunk)


class SyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        Handler.files = {}
        Handler.hits = []
        Handler.fail_after = {}
        self.cfg = config._merge(config.DEFAULTS, {
            "playlist": {"url": self.base + "/sig/playlist.json", "timeout_sec": 5},
            "storage": {"dir": self.tmp.name, "delete_grace_min": 0},
        })
        self.paths = Paths(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, playlist):
        Handler.files["/sig/playlist.json"] = json.dumps(playlist).encode()

    def manifest(self):
        return json.loads(self.paths.manifest.read_text())

    def test_download_with_sha_and_relative_url(self):
        video = os.urandom(300_000)
        Handler.files["/sig/v/a.mp4"] = video
        self.publish({"mode": "shuffle", "items": [
            {"url": "v/a.mp4", "sha256": hashlib.sha256(video).hexdigest(), "title": "A", "repeat": 2}]})
        self.assertEqual(sync.run(self.cfg), 0)
        m = self.manifest()
        self.assertEqual(m["mode"], "shuffle")
        self.assertEqual(len(m["items"]), 1)
        self.assertEqual(Path(m["items"][0]["path"]).read_bytes(), video)
        self.assertEqual(m["items"][0]["repeat"], 2)
        # 2 回目はダウンロードしない
        Handler.hits.clear()
        self.assertEqual(sync.run(self.cfg), 0)
        self.assertEqual([h[0] for h in Handler.hits], ["/sig/playlist.json"])

    def test_resume_after_interruption(self):
        video = os.urandom(500_000)
        Handler.files["/v.mp4"] = video
        Handler.fail_after["/v.mp4"] = 200_000
        self.publish({"items": [{"url": "/v.mp4", "sha256": hashlib.sha256(video).hexdigest()}]})
        sync.run(self.cfg)
        self.assertEqual(self.manifest()["items"], [])
        sync.run(self.cfg)
        ranges = [h[1].get("Range") for h in Handler.hits if h[0] == "/v.mp4"]
        self.assertTrue(ranges[-1] and ranges[-1].startswith("bytes="))
        self.assertEqual(Path(self.manifest()["items"][0]["path"]).read_bytes(), video)

    def test_bad_sha_rejected(self):
        Handler.files["/v.mp4"] = b"x" * 1000
        self.publish({"items": [{"url": "/v.mp4", "sha256": "0" * 64}]})
        self.assertEqual(sync.run(self.cfg), 1)
        self.assertEqual(self.manifest()["items"], [])

    def test_etag_revalidation_and_update(self):
        Handler.files["/v.mp4"] = b"old" * 100
        self.publish({"items": ["/v.mp4"]})
        sync.run(self.cfg)
        path = Path(self.manifest()["items"][0]["path"])
        self.assertEqual(path.read_bytes(), b"old" * 100)
        sync.run(self.cfg)  # 304
        self.assertEqual(path.read_bytes(), b"old" * 100)
        Handler.files["/v.mp4"] = b"new" * 100
        sync.run(self.cfg)
        self.assertEqual(path.read_bytes(), b"new" * 100)

    def test_playlist_unreachable_keeps_manifest(self):
        Handler.files["/v.mp4"] = b"v" * 10
        self.publish({"items": ["/v.mp4"]})
        sync.run(self.cfg)
        before = self.paths.manifest.read_text()
        Handler.files.pop("/sig/playlist.json")
        self.assertEqual(sync.run(self.cfg), 1)
        self.assertEqual(self.paths.manifest.read_text(), before)

    def test_invalid_playlist_keeps_manifest(self):
        Handler.files["/sig/playlist.json"] = b"{broken"
        self.assertEqual(sync.run(self.cfg), 1)
        self.assertFalse(self.paths.manifest.exists())
        self.publish({"mode": "random", "items": []})
        self.assertEqual(sync.run(self.cfg), 1)

    def test_removed_items_deleted_after_grace(self):
        Handler.files["/a.mp4"] = b"a" * 10
        Handler.files["/b.mp4"] = b"b" * 10
        self.publish({"items": ["/a.mp4", "/b.mp4"]})
        sync.run(self.cfg)
        self.assertEqual(len(list(self.paths.media.glob("*.mp4"))), 2)
        self.publish({"items": ["/b.mp4"]})
        self.cfg["storage"]["delete_grace_min"] = 30
        sync.run(self.cfg)
        self.assertEqual(len(list(self.paths.media.glob("*.mp4"))), 2)  # 猶予中
        self.cfg["storage"]["delete_grace_min"] = 0
        sync.run(self.cfg)
        self.assertEqual(len(list(self.paths.media.glob("*.mp4"))), 1)

    def test_remote_schedule_validated(self):
        self.publish({"items": [], "schedule": {"mon": ["bad"]}})
        self.assertEqual(sync.run(self.cfg), 1)


# ---------------------------------------------------------------- Player のイベント処理


class FakeMpv:
    def __init__(self):
        self.sent = []

    def loadfile(self, item, flags):
        self.sent.append(("loadfile", Path(item["path"]).name, flags))

    def command(self, *args):
        self.sent.append(args)

    def send(self, payload):
        self.sent.append(("send", payload["command"]["url"]))

    def alive(self):
        return True


class PlayerEventTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        cfg = config._merge(config.DEFAULTS, {"storage": {"dir": self.tmp.name}, "display": {"methods": []}})
        self.player = Player(cfg)
        self.player.mpv = FakeMpv()
        for n in "ab":
            (d / f"{n}.mp4").write_text(n)
        sync.write_json_atomic(self.player.paths.manifest, {
            "mode": "sequential", "items": [{"path": str(d / f"{n}.mp4")} for n in "ab"]})
        self.player.check_manifest()

    def tearDown(self):
        self.tmp.cleanup()

    def test_idle_then_advance(self):
        p, m = self.player, self.player.mpv
        p.on_event({"event": "property-change", "name": "idle-active", "data": True})
        self.assertEqual(m.sent[-1], ("loadfile", "a.mp4", "replace"))
        p.on_event({"event": "start-file"})
        self.assertEqual(m.sent[-2:], [("playlist-clear",), ("loadfile", "b.mp4", "append-play")])
        self.assertEqual(Path(p.current["path"]).name, "a.mp4")
        p.on_event({"event": "start-file"})
        self.assertEqual(m.sent[-1], ("loadfile", "a.mp4", "append-play"))
        self.assertEqual(Path(p.current["path"]).name, "b.mp4")

    def test_override(self):
        p = self.player
        p.paths.override.write_text("off\n")
        self.assertFalse(p.desired_on())
        p.paths.override.write_text("on")
        self.assertTrue(p.desired_on())

    def test_remote_schedule_preferred(self):
        p = self.player
        p.manifest["schedule"] = {"mon": ["00:00-24:00"]}
        self.assertIs(p.effective_schedule(), p.manifest["schedule"])
        p.cfg["schedule"]["allow_remote"] = False
        self.assertIs(p.effective_schedule(), p.cfg["schedule"])


class ConfigTest(unittest.TestCase):
    def test_example_config_loads(self):
        cfg = config.load(Path(__file__).parent.parent / "config" / "signage.toml.example")
        schedule.validate(cfg["schedule"])
        self.assertTrue(cfg["playlist"]["url"].startswith("https://"))

    def test_example_playlist_parses(self):
        data = json.loads((Path(__file__).parent.parent / "examples" / "playlist.json").read_text())
        pl = sync.parse_playlist(data, "https://example.ac.jp/signage/playlist.json")
        self.assertEqual(pl["items"][0]["url"], "https://example.ac.jp/signage/videos/intro.mp4")


if __name__ == "__main__":
    unittest.main()
