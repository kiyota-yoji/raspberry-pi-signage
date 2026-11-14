"""Web 上の再生リスト JSON を取得し、指定された動画をダウンロードする。

処理の流れ:
  1. 再生リスト JSON を取得（失敗したら前回のマニフェストをそのまま使う）
  2. 各動画をダウンロード（途中で切れたら次回 Range で再開。sha256 があれば検証）
  3. 手元に揃った動画だけでマニフェストを作り、原子的に置き換える
  4. JSON から外れた動画は猶予時間の経過後に削除
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import http.client
import json
import logging
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import schedule as sched
from .config import Paths

log = logging.getLogger("signage.sync")

USER_AGENT = "raspberry-pi-signage/1.0"
CHUNK = 1024 * 1024
VALID_MODES = ("sequential", "shuffle")
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".ts", ".avi", ".jpg", ".jpeg", ".png"}


class SyncError(Exception):
    pass


def write_json_atomic(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


class Fetcher:
    def __init__(self, cfg: dict):
        pl = cfg["playlist"]
        self.timeout = pl.get("timeout_sec", 30)
        self.auth = None
        if pl.get("username"):
            token = f"{pl['username']}:{pl.get('password', '')}".encode()
            self.auth = "Basic " + base64.b64encode(token).decode()

    def open(self, url: str, headers: dict | None = None):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        if self.auth:
            req.add_header("Authorization", self.auth)
        return urllib.request.urlopen(req, timeout=self.timeout)


# ---------------------------------------------------------------- 再生リスト


def parse_playlist(data: dict, base_url: str) -> dict:
    """再生リスト JSON を検証・正規化する。不正なら SyncError。"""
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise SyncError('再生リスト JSON には "items" 配列が必要です')
    mode = data.get("mode", "sequential")
    if mode not in VALID_MODES:
        raise SyncError(f'"mode" は {VALID_MODES} のいずれかです: {mode!r}')

    items = []
    for i, raw in enumerate(data["items"]):
        if isinstance(raw, str):
            raw = {"url": raw}
        if not isinstance(raw, dict) or not raw.get("url"):
            raise SyncError(f"items[{i}] に url がありません")
        repeat = raw.get("repeat", 1)
        if not isinstance(repeat, int) or repeat < 1:
            raise SyncError(f"items[{i}].repeat は 1 以上の整数です")
        sha = raw.get("sha256")
        if sha is not None and (not isinstance(sha, str) or len(sha) != 64):
            raise SyncError(f"items[{i}].sha256 は 64 桁の 16 進文字列です")
        for key in ("valid_from", "valid_until"):
            if raw.get(key):
                try:
                    sched._parse_dt(raw[key])
                except ValueError as e:
                    raise SyncError(f"items[{i}].{key} の日時が不正です: {raw[key]!r}") from e
        duration = raw.get("duration")  # 静止画の表示秒数
        items.append({
            "url": urllib.parse.urljoin(base_url, raw["url"]),
            "title": raw.get("title", ""),
            "sha256": sha.lower() if sha else None,
            "repeat": repeat,
            "valid_from": raw.get("valid_from"),
            "valid_until": raw.get("valid_until"),
            "duration": duration,
        })

    remote_schedule = data.get("schedule")
    if remote_schedule is not None:
        try:
            sched.validate(remote_schedule)
        except (ValueError, TypeError, AttributeError) as e:
            raise SyncError(f'"schedule" が不正です: {e}') from e

    return {"mode": mode, "items": items, "schedule": remote_schedule}


def fetch_playlist(cfg: dict, fetcher: Fetcher) -> tuple[dict, dict]:
    url = cfg["playlist"]["url"]
    if not url:
        raise SyncError("設定 playlist.url が空です")
    try:
        with fetcher.open(url, {"Cache-Control": "no-cache"}) as resp:
            raw = json.load(resp)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SyncError(f"再生リストを取得できません: {url}: {e}") from e
    except json.JSONDecodeError as e:
        raise SyncError(f"再生リストが JSON ではありません: {e}") from e
    return parse_playlist(raw, url), raw


# ---------------------------------------------------------------- 動画ファイル


def local_name(item: dict) -> str:
    """保存ファイル名。sha256 があれば内容で、無ければ URL で決める。"""
    ext = PurePosixPath(urllib.parse.urlparse(item["url"]).path).suffix.lower()
    if ext not in VIDEO_EXTS:
        ext = ".mp4"
    key = item["sha256"] or "u" + hashlib.sha256(item["url"].encode()).hexdigest()
    return key[:24] + ext


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def download(item: dict, dest: Path, fetcher: Fetcher) -> bool:
    """動画を dest に用意する。用意できたら True。

    - sha256 指定あり: 一度揃えば再取得しない。途中ファイル (.part) からは Range で再開。
    - sha256 指定なし: ETag / Last-Modified で更新を確認し、変わっていれば取り直す。
    """
    meta_path = dest.with_name(dest.name + ".meta")
    part = dest.with_name(dest.name + ".part")

    if item["sha256"] and dest.exists():
        return True

    headers = {}
    meta = read_json(meta_path, {}) or {}
    if not item["sha256"] and dest.exists():
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = meta["last_modified"]
        part.unlink(missing_ok=True)

    offset = 0
    if item["sha256"] and part.exists():
        offset = part.stat().st_size
        if offset:
            headers["Range"] = f"bytes={offset}-"

    try:
        resp = fetcher.open(item["url"], headers)
    except urllib.error.HTTPError as e:
        e.close()
        if e.code == 304:
            return True
        if e.code == 416 and offset:  # 途中ファイルが既に全量 → 検証へ
            resp = None
        else:
            log.warning("ダウンロード失敗 %s: HTTP %s", item["url"], e.code)
            return dest.exists()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("ダウンロード失敗 %s: %s", item["url"], e)
        return dest.exists()

    if resp is not None:
        with resp:
            mode = "ab" if offset and resp.status == 206 else "wb"
            if mode == "wb":
                offset = 0
            log.info("ダウンロード中 %s%s", item["url"], f"（{offset} バイト目から再開）" if offset else "")
            try:
                with open(part, mode) as f:
                    shutil.copyfileobj(resp, f, CHUNK)
            except (TimeoutError, OSError, http.client.HTTPException) as e:
                log.warning("ダウンロード中断 %s: %s（次回再開）", item["url"], e)
                if not item["sha256"]:
                    part.unlink(missing_ok=True)
                return dest.exists()
            new_meta = {
                "url": item["url"],
                "etag": resp.headers.get("ETag"),
                "last_modified": resp.headers.get("Last-Modified"),
            }
            length = resp.headers.get("Content-Length")
            expected = (offset + int(length)) if length is not None else None
        actual = part.stat().st_size
        if expected is not None and actual != expected:
            if actual < expected and item["sha256"]:
                log.warning("ダウンロード中断 %s: %d/%d バイト（次回再開）", item["url"], actual, expected)
            else:
                log.warning("サイズ不一致 %s: %d/%d バイト（次回再取得）", item["url"], actual, expected)
                part.unlink(missing_ok=True)
            return dest.exists()
    else:
        new_meta = meta

    if item["sha256"]:
        actual = file_sha256(part)
        if actual != item["sha256"]:
            log.error("sha256 不一致 %s: 期待 %s / 実際 %s", item["url"], item["sha256"], actual)
            part.unlink(missing_ok=True)
            return False

    os.replace(part, dest)
    write_json_atomic(meta_path, new_meta)
    log.info("取得完了 %s -> %s", item["url"], dest.name)
    return True


# ---------------------------------------------------------------- 後片付け


def cleanup(paths: Paths, keep: set[str], grace_min: float, now: float | None = None) -> None:
    """マニフェストに無いファイルを、猶予時間が過ぎてから削除する。"""
    now = now if now is not None else time.time()
    pending = read_json(paths.pending_delete, {}) or {}
    new_pending = {}
    for f in paths.media.iterdir():
        if not f.is_file():
            continue
        base = f.name.removesuffix(".meta").removesuffix(".part").removesuffix(".tmp")
        if base in keep:
            continue
        first_seen = pending.get(f.name, now)
        if now - first_seen >= grace_min * 60:
            log.info("不要になったファイルを削除: %s", f.name)
            f.unlink(missing_ok=True)
        else:
            new_pending[f.name] = first_seen
    write_json_atomic(paths.pending_delete, new_pending)


# ---------------------------------------------------------------- 本体


def run(cfg: dict) -> int:
    paths = Paths(cfg)
    paths.ensure()
    with open(paths.lock, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("別の同期処理が実行中のため終了します")
            return 0
        return _run_locked(cfg, paths)


def _run_locked(cfg: dict, paths: Paths) -> int:
    fetcher = Fetcher(cfg)
    try:
        playlist, raw = fetch_playlist(cfg, fetcher)
    except SyncError as e:
        log.error("%s（前回のマニフェストで再生を続けます）", e)
        return 1
    write_json_atomic(paths.remote_cache, raw)

    now = datetime.now()
    manifest_items = []
    keep: set[str] = set()
    failed = 0
    for item in playlist["items"]:
        name = local_name(item)
        # 掲出期間が終わったものはダウンロードしない
        if item["valid_until"] and not sched.in_window(now, None, item["valid_until"]):
            continue
        keep.add(name)
        if download(item, paths.media / name, fetcher):
            manifest_items.append({
                "path": str(paths.media / name),
                "url": item["url"],
                "title": item["title"],
                "repeat": item["repeat"],
                "valid_from": item["valid_from"],
                "valid_until": item["valid_until"],
                "duration": item["duration"],
            })
        else:
            failed += 1

    manifest = {
        "generated_at": now.isoformat(timespec="seconds"),
        "source": cfg["playlist"]["url"],
        "mode": playlist["mode"],
        "schedule": playlist["schedule"],
        "items": manifest_items,
    }
    old = read_json(paths.manifest, {}) or {}
    if {k: v for k, v in old.items() if k != "generated_at"} != {
        k: v for k, v in manifest.items() if k != "generated_at"
    }:
        write_json_atomic(paths.manifest, manifest)
        log.info("マニフェストを更新しました（%d 本）", len(manifest_items))
    else:
        log.info("再生リストに変更はありません（%d 本）", len(manifest_items))

    cleanup(paths, keep, cfg["storage"]["delete_grace_min"])
    return 1 if failed else 0
