# Raspberry Pi デジタルサイネージ

Raspberry Pi を HDMI ディスプレイにつなぎ、Web サイトに置いた再生リスト（JSON）に従って動画を流し続けるサイネージです。

- **学内 Wi-Fi に自動接続**（WPA2-Personal / WPA2-Enterprise・eduroam）。切れたら自動で再接続
- **再生リスト JSON を定期取得**し、指定された動画を自動ダウンロード（途中で切れても再開、sha256 で検証）
- **順番再生／シャッフル、繰り返し回数、掲出期間**を JSON で指定
- **決まった時刻にディスプレイを ON/OFF**（HDMI-CEC でテレビの電源を操作／モニタは信号停止で省電力）。夜間は再生しない
- ネットワークが切れても、手元にダウンロード済みの動画で再生を続ける

## 構成

```
 Web サーバー                       Raspberry Pi
 ┌──────────────┐  10分ごと   ┌───────────────────────────────┐
 │ playlist.json │◀──────────│ signage-sync (timer)           │
 │ videos/*.mp4  │──────────▶│   動画を /var/lib/signage/media │
 └──────────────┘            │   manifest.json を更新          │
                             │            │                    │
                             │ signage-player (常駐)            │
                             │   manifest を監視 → mpv で再生   │──HDMI──▶ ディスプレイ
                             │   時刻を見て点灯/消灯 (CEC 等)    │
                             │                                 │
                             │ signage-netwatch (5分ごと)       │
                             │   疎通がなければ Wi-Fi 再接続     │
                             └───────────────────────────────┘
```

| ファイル | 役割 |
|---|---|
| `signage/sync.py` | 再生リスト JSON の取得、動画のダウンロード・検証、古い動画の削除 |
| `signage/player.py` | mpv の制御、再生順の決定、スケジュールによる点灯・消灯 |
| `signage/schedule.py` | 点灯時間帯・掲出期間の判定 |
| `signage/display.py` | ディスプレイの ON/OFF（CEC / fb_blank / vcgencmd） |
| `signage/netwatch.py` | Wi-Fi 疎通監視と段階的な復旧 |
| `signage/bootimport.py` | 起動時に SD カードの boot 領域から設定・Wi-Fi を取り込む |
| `scripts/setup-wifi.sh` | Wi-Fi 接続の登録 |
| `scripts/install.sh` | インストール |
| `config/signage.toml.example` | 設定ファイルの雛形 |
| `examples/playlist.json` | 再生リスト JSON の例 |
| `examples/wifi.txt` | SD カードから Wi-Fi を設定するときの雛形 |

外部ライブラリは使っていません（Python 標準ライブラリ + mpv + cec-ctl）。

## 必要なもの

- Raspberry Pi 4 / 5（Pi 3 でも動きますが 1080p の再生は H.264 に限られます）
- **Raspberry Pi OS Lite（64-bit, Bookworm 以降）** … デスクトップ版でも動きますが、インストール時にコンソール起動に切り替えます
- microSD 32GB 以上（動画の容量に応じて）
- HDMI ディスプレイ。夜間に電源ごと切りたい場合は **HDMI-CEC 対応**のテレビ／業務用ディスプレイ

## セットアップ

### 1. OS の書き込み

Raspberry Pi Imager で Raspberry Pi OS Lite を書き込みます。詳細設定で **ホスト名・ユーザー・SSH・タイムゾーン（Asia/Tokyo）** を設定しておくと便利です。
初回の作業用には有線 LAN かスマートフォンのテザリングを使うのが確実です（学内 Wi-Fi の設定は手順 3 で行います）。

### 2. インストール

```bash
sudo apt-get install -y git
git clone <このリポジトリ> ~/raspberry-pi-signage
cd ~/raspberry-pi-signage
sudo ./scripts/install.sh --boot-tweaks --force-hdmi 1920x1080@60
```

- `--boot-tweaks` … 起動時のロゴ・カーソルを消し、コンソールの自動消灯を止めます
- `--force-hdmi 1920x1080@60` … ディスプレイの電源が切れている状態で起動しても HDMI 出力を有効にします。**CEC で夜間に電源を切る場合は必須**です（指定しないと、朝にテレビを点けても映らないことがあります）。ディスプレイの解像度に合わせてください

### 3. 学内 Wi-Fi の設定

**パスフレーズ方式（WPA2/WPA3-Personal）**

```bash
sudo ./scripts/setup-wifi.sh --ssid CAMPUS-WIFI
```

**ID・パスワード方式（WPA2-Enterprise / eduroam）**

```bash
sudo ./scripts/setup-wifi.sh --ssid eduroam --eap peap --identity signage01@example.ac.jp --anonymous-identity anonymous@example.ac.jp --ca-cert /etc/ssl/certs/<大学指定のCA>.pem --domain-suffix-match radius.example.ac.jp
```

パスワードは実行時に入力を求められます。認証方式（PEAP/TTLS、内部認証 MSCHAPv2/PAP）、CA 証明書、RADIUS サーバー名は大学の情報システム部門の案内に従ってください。

- 接続は NetworkManager に登録され、起動時の自動接続・切断時の再接続が行われます
- Wi-Fi の省電力機能を無効化し、MAC アドレスのランダム化も止めます（端末登録制のネットワーク向け）。最後に表示される MAC アドレスを登録に使ってください
- サイネージ用には個人アカウントではなく、**機器用アカウント**の発行を依頼するのがおすすめです
- ブラウザでログインする方式（キャプティブポータル）の Wi-Fi には対応していません。機器を MAC 認証で通してもらえないか相談してください

### 4. 設定ファイル

```bash
sudo nano /etc/signage/signage.toml
```

最低限 `[playlist] url` と `[schedule]` を設定し、`[network] connection` が手順 3 の接続名（既定 `campus-wifi`）になっていることを確認します。各項目の説明は [config/signage.toml.example](config/signage.toml.example) を参照してください。

設定と再生リストを確認します:

```bash
signagectl check
```

### 5. 開始

```bash
sudo systemctl start signage-sync.service signage-player.service signage-sync.timer signage-netwatch.timer
```

以後は電源を入れるだけで自動的に起動します。

## 再生リスト JSON

Web サーバーに置き、その URL を `playlist.url` に設定します。例: [examples/playlist.json](examples/playlist.json)

```json
{
  "mode": "sequential",
  "items": [
    { "title": "大学紹介", "url": "videos/intro.mp4", "sha256": "9f86d0…(64桁)", "repeat": 1 },
    { "title": "OC告知", "url": "videos/oc.mp4", "repeat": 2,
      "valid_from": "2026-11-01T00:00:00+09:00", "valid_until": "2026-12-20" },
    { "title": "お知らせ", "url": "images/notice.png", "duration": 15 }
  ],
  "schedule": { "mon": ["08:00-20:00"], "sat": ["09:00-17:00"], "off_dates": ["2026-12-29"] }
}
```

| キー | 必須 | 説明 |
|---|---|---|
| `mode` | | `sequential`（上から順に、既定）または `shuffle`（1 周ごとに順番を入れ替え。同じ動画は連続しない） |
| `items[].url` | ✔ | 動画の URL。相対パスは JSON の URL を基準に解決。`items` には URL の文字列だけを並べても可 |
| `items[].sha256` | | ファイルの SHA-256。**指定を推奨**: ダウンロード後に検証し、通信が切れても続きから再開します。内容を差し替えるときは別の値になります |
| `items[].repeat` | | 1 周の中で続けて再生する回数（既定 1） |
| `items[].valid_from` / `valid_until` | | 掲出期間。日付のみの `valid_until` はその日の終わりまで。期間外は再生せず、期限切れの動画はダウンロードもしません |
| `items[].duration` | | 静止画（jpg/png）の表示秒数（既定 10） |
| `items[].title` | | 表示・ログ用の名前 |
| `schedule` | | 点灯スケジュール（書式は設定ファイルの `[schedule]` と同じ）。設定ファイル側で `allow_remote = true` のとき、こちらが優先されます。省略時は設定ファイルの値を使います |

**更新のしかた**: JSON を書き換えるだけです。サイネージは 10 分ごとに JSON を確認し、新しい動画をダウンロードし終えたものから、**今の動画が終わった次の動画から**新しいリストに切り替わります。リストから外した動画は 30 分の猶予の後に削除されます。

- `sha256` なしの動画は、ETag / Last-Modified で毎回更新を確認し、変わっていれば取り直します
- JSON の取得に失敗したり、JSON が不正（書式エラー等）な場合は、**前回の内容のまま再生を続けます**
- Basic 認証が必要な場所に置く場合は設定ファイルの `username` / `password` を設定します

SHA-256 の求め方:

```bash
shasum -a 256 intro.mp4
```

### 動画の推奨形式

ハードウェアデコードの都合上、次の形式を推奨します。

| 機種 | 推奨 |
|---|---|
| Raspberry Pi 5 | H.265 (HEVC) 1080p/4K。H.264 1080p も CPU で再生可 |
| Raspberry Pi 4 | H.264 1080p30 または H.265 1080p/4K |

変換例（ffmpeg）:

```bash
ffmpeg -i input.mov -c:v libx264 -profile:v high -pix_fmt yuv420p -vf scale=1920:1080 -r 30 -b:v 8M -c:a aac -b:a 128k -movflags +faststart output.mp4
```

## スケジュール（点灯・消灯）

```toml
[schedule]
mon = ["08:00-20:00"]
tue = ["08:00-12:00", "13:00-20:00"]   # 複数の時間帯
fri = ["08:00-20:00", "22:00-02:00"]   # 日付またぎも可
sun = []                                # 終日 OFF
off_dates = ["2026-12-29", "2027-01-01"]
```

時間帯の外になると mpv を停止し、ディスプレイを消灯します。消灯方法は `[display] methods` で選びます。

| 方法 | 対象 | 動作 |
|---|---|---|
| `cec` | テレビ・業務用ディスプレイ | HDMI-CEC でスタンバイ／電源 ON。ディスプレイ側で CEC（メーカーにより「Anynet+」「ブラビアリンク」「ビエラリンク」等）を有効にしてください |
| `fb_blank` | PC モニタ | HDMI 信号を止め、モニタを省電力モードにする |
| `vcgencmd` | 旧環境 | Legacy/FKMS ドライバの場合のみ |

既定は `["cec", "fb_blank"]` で、両方を試します。誰かが夜間にテレビを手動で点けても、`reassert_off_min`（既定 30 分）ごとに再度消します。

**注意**: Raspberry Pi 自体は夜間も起動したままです（ダウンロードや監視は続きます）。

## 運用コマンド

```bash
signagectl status            # 現在の状態（点灯中か、再生中の動画、本数）
signagectl override off      # スケジュールを無視して消灯（メンテナンス時など）
signagectl override on       # スケジュールを無視して点灯
signagectl override auto     # スケジュールどおりに戻す
signagectl sync              # 今すぐ再生リストを取得
signagectl check             # 設定と再生リストの検証
```

ログ:

```bash
journalctl -u signage-player -u signage-sync -u signage-netwatch -u signage-bootimport -f
```

### SSH で設定を変更する

```bash
signagectl override off                  # 表示を止める（再起動しても消灯のまま）
sudo nano /etc/signage/signage.toml      # 編集
signagectl check                         # 確認
sudo systemctl restart signage-player    # [schedule] [display] [player] の変更を反映
signagectl override auto                 # スケジュールどおりに戻す
```

`[playlist]` `[storage]` `[network]` は次の同期・監視（10 分以内）で自動的に反映されます。

## SD カードから設定する（ネットワーク不要）

Raspberry Pi の電源を切って SD カードを Mac / Windows に挿すと、`bootfs` という名前のドライブが見えます（Pi の中では `/boot/firmware`）。ここに次のファイルを置いて SD カードを戻し、電源を入れると、**再生が始まる前に**取り込まれます。

| 置くファイル | 起動時の動作 |
|---|---|
| `signage-maintenance`（または `signage-maintenance.txt`） | **メンテナンスモード**。再生せず、画面にログインプロンプトを出します。キーボードをつないでログインし、作業できます。ファイルがある限り、何度起動しても再生しません |
| `signage.toml` | 設定ファイル。検証して `/etc/signage/signage.toml` に反映し、`signage.toml.applied` に改名します。書式が不正なら反映せず `signage.toml.error` に改名します（前の設定のまま動きます） |
| `wifi.txt` | Wi-Fi の接続設定を登録します。パスワードを含むため、**登録後に自動で削除**されます。雛形: [examples/wifi.txt](examples/wifi.txt) |

結果は同じ場所の **`signage-result.txt`** に書き出されるので、SD カードを Mac に挿せば確認できます（Wi-Fi が接続できたか、IP アドレス、MAC アドレスなど）。

**設定を変えるときの手順の例**

1. 前回反映した `signage.toml.applied` をコピーして `signage.toml` という名前にし、編集する（初回は [config/signage.toml.example](config/signage.toml.example) から作る）
2. SD カードを Pi に戻して電源を入れる
3. 必要なら、もう一度 SD カードを抜いて `signage-result.txt` を確認する

**メンテナンスモードを終えるには**、SD カードの `signage-maintenance` を消して起動し直すか、Pi にログインして次を実行します（再起動は不要。5 秒以内に再生が始まります）。

```bash
sudo rm /boot/firmware/signage-maintenance*
```

**注意**

- `--boot-tweaks` でインストールした場合、ログイン画面にカーソルが表示されません。入力自体はできます
- boot 領域のファイルは Pi のどのユーザーからも読めます。設定ファイルに Basic 認証のパスワードを書いた場合、`signage.toml.applied` を削除しておくと安全です
- テキストエディタは UTF-8 で保存してください（Windows のメモ帳の BOM 付き UTF-8・改行 CRLF も可）

## Wi-Fi の自動復旧

`signage-netwatch` が 5 分ごとに `playlist.url`（または `network.check_url`）へ接続できるか確認し、失敗が続くと段階的に復旧を試みます。

1. 2 回連続失敗（約 10 分）→ `nmcli connection up` で張り直し
2. 4 回連続失敗（約 20 分）→ NetworkManager を再起動
3. `reboot_after` を設定した場合 → OS を再起動（既定は無効）

## トラブルシューティング

| 症状 | 確認すること |
|---|---|
| 何も映らない | `journalctl -u signage-player` を確認。`signagectl status` で `state` が `on` か。デスクトップ環境が動いていると mpv が画面を使えません（`systemctl get-default` が `multi-user.target` か） |
| Pi 5 で mpv が起動しない | 映像出力のデバイスを指定: 設定の `mpv_extra_args = ["--drm-device=/dev/dri/card1"]` |
| カクつく | 動画を推奨形式に変換。Pi 5 で H.264 4K は再生できません |
| 朝にテレビが点いても映らない | `install.sh --force-hdmi` を指定して再起動 |
| CEC で電源が切れない | ディスプレイの CEC 設定を有効化。`cec-ctl -d /dev/cec0 --playback` の後 `cec-ctl -d /dev/cec0 --to 0 --standby` を手で実行して確認。Pi 4 で HDMI1 端子に挿した場合は `cec_device = "/dev/cec1"` |
| 動画がダウンロードされない | `signagectl check` で JSON を確認。`journalctl -u signage-sync` で sha256 不一致や HTTP エラーが出ていないか |
| Wi-Fi につながらない | `nmcli connection show campus-wifi`、`journalctl -u NetworkManager`。国コード（`--country JP`）・認証方式・CA 証明書・MAC 登録を確認 |

## 開発

テスト（macOS / Linux、Python 3.11 以上）:

```bash
python3 -m unittest discover -s tests -t .
```

テストはスケジュール判定・再生順・ダウンロード（再開・検証・更新確認・削除）・プレイヤーのイベント処理を確認します。mpv と CEC の実機動作はテストに含まれていないので、実機で確認してください。
