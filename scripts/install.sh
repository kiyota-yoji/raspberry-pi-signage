#!/bin/bash
# Raspberry Pi サイネージのインストール（Raspberry Pi OS Lite, Bookworm 以降）
#
#   sudo ./scripts/install.sh [--boot-tweaks] [--force-hdmi 1920x1080@60]
#
#   --boot-tweaks   起動時のロゴ・カーソル・コンソール自動消灯を止める（cmdline.txt を編集）
#   --force-hdmi M  テレビが電源オフでも HDMI 出力を有効にする（CEC で夜間に電源を切る場合に推奨）
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
BOOT_TWEAKS=no
FORCE_HDMI=""
while [ $# -gt 0 ]; do
  case "$1" in
    --boot-tweaks) BOOT_TWEAKS=yes; shift ;;
    --force-hdmi) FORCE_HDMI="$2"; shift 2 ;;
    *) echo "不明な引数: $1" >&2; exit 1 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "sudo で実行してください" >&2; exit 1; }

echo "==> パッケージをインストール"
apt-get update
apt-get install -y --no-install-recommends mpv v4l-utils python3

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "Python 3.11 以上が必要です（Raspberry Pi OS Bookworm 以降）" >&2; exit 1; }

echo "==> ユーザー signage を作成"
if ! id signage >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/signage --shell /usr/sbin/nologin signage
fi
usermod -aG video,render,audio signage

echo "==> プログラムを配置"
install -d /opt/signage
rm -rf /opt/signage/signage
cp -r "$SRC/signage" /opt/signage/
find /opt/signage -name '__pycache__' -prune -exec rm -rf {} +
install -d /opt/signage/scripts
install -m 755 "$SRC/scripts/setup-wifi.sh" /opt/signage/scripts/setup-wifi.sh

install -d -m 755 /etc/signage
if [ ! -e /etc/signage/signage.toml ]; then
  install -m 640 -g signage "$SRC/config/signage.toml.example" /etc/signage/signage.toml
  echo "    /etc/signage/signage.toml を作成しました（playlist.url などを編集してください）"
fi
install -d -o signage -g signage -m 755 /var/lib/signage

cat > /usr/local/bin/signagectl <<'EOF'
#!/bin/sh
# signage の操作コマンド（例: signagectl status / signagectl override off / signagectl sync）
cd /var/lib/signage 2>/dev/null || cd /
case "$1" in
  netwatch) exec sudo env PYTHONPATH=/opt/signage python3 -m signage "$@" ;;
  *) exec sudo -u signage env PYTHONPATH=/opt/signage python3 -m signage "$@" ;;
esac
EOF
chmod 755 /usr/local/bin/signagectl

echo "==> systemd に登録"
install -m 644 "$SRC"/systemd/*.service "$SRC"/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable signage-bootimport.service signage-player.service signage-sync.timer signage-netwatch.timer

if [ "$(systemctl get-default)" = "graphical.target" ]; then
  echo "    デスクトップ環境が起動する設定のため、コンソール起動に切り替えます（mpv が画面を直接使うため）"
  systemctl set-default multi-user.target
fi

CMDLINE=/boot/firmware/cmdline.txt
[ -e "$CMDLINE" ] || CMDLINE=/boot/cmdline.txt
add_cmdline() {
  local opt="$1"
  if ! grep -qw -- "${opt%%=*}" "$CMDLINE"; then
    sed -i "1 s|\$| $opt|" "$CMDLINE"
  fi
}
if [ "$BOOT_TWEAKS" = yes ] || [ -n "$FORCE_HDMI" ]; then
  cp -n "$CMDLINE" "$CMDLINE.signage-backup" || true
fi
if [ "$BOOT_TWEAKS" = yes ]; then
  echo "==> 起動画面の設定 ($CMDLINE)"
  add_cmdline consoleblank=0
  add_cmdline vt.global_cursor_default=0
  add_cmdline logo.nologo
fi
if [ -n "$FORCE_HDMI" ]; then
  echo "==> HDMI 出力を常時有効化 ($CMDLINE)"
  if grep -q 'video=HDMI-A-1:' "$CMDLINE"; then
    sed -i "1 s|video=HDMI-A-1:[^ ]*|video=HDMI-A-1:${FORCE_HDMI}D|" "$CMDLINE"
  else
    sed -i "1 s|\$| video=HDMI-A-1:${FORCE_HDMI}D|" "$CMDLINE"
  fi
fi

echo
echo "インストールしました。次の手順:"
echo "  1. Wi-Fi 設定:     sudo $SRC/scripts/setup-wifi.sh --ssid <SSID> ..."
echo "                    （または SD カードの boot 領域に wifi.txt を置いて再起動）"
echo "  2. 設定ファイル:   sudo nano /etc/signage/signage.toml"
echo "  3. 設定の確認:     signagectl check"
echo "  4. 開始:           sudo systemctl start signage-sync.service signage-player.service signage-sync.timer signage-netwatch.timer"
[ "$BOOT_TWEAKS" = yes ] || [ -n "$FORCE_HDMI" ] && echo "  ※ cmdline.txt を変更したので再起動してください: sudo reboot"
exit 0
