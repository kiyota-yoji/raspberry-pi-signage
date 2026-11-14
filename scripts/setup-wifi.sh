#!/bin/bash
# 学内 Wi-Fi の接続設定を NetworkManager に登録する（自動接続・切断時の自動再接続あり）
#
# WPA2/WPA3-Personal（パスフレーズ）:
#   sudo ./setup-wifi.sh --ssid CAMPUS-WIFI
#
# WPA2-Enterprise / eduroam（ID・パスワード, PEAP/MSCHAPv2）:
#   sudo ./setup-wifi.sh --ssid eduroam --eap peap --identity signage01@example.ac.jp \
#        --anonymous-identity anonymous@example.ac.jp \
#        --ca-cert /etc/ssl/certs/ISRG_Root_X1.pem --domain-suffix-match radius.example.ac.jp
#
# パスワード／パスフレーズは --password で渡さなければ入力を求める（シェル履歴に残さないため推奨）
# 標準入力から渡すこともできる: printf '%s\n' "$PW" | sudo ./setup-wifi.sh --ssid ...
set -euo pipefail

NAME="campus-wifi"
SSID=""
PSK=""
EAP=""
PHASE2="mschapv2"
IDENTITY=""
ANON=""
PASSWORD=""
CA_CERT=""
DOMAIN=""
COUNTRY="JP"
HIDDEN="no"
PRIORITY=10

usage() { sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --ssid) SSID="$2"; shift 2 ;;
    --password) PASSWORD="$2"; shift 2 ;;
    --eap) EAP="$2"; shift 2 ;;                 # peap / ttls
    --phase2) PHASE2="$2"; shift 2 ;;           # mschapv2 / pap など
    --identity) IDENTITY="$2"; shift 2 ;;
    --anonymous-identity) ANON="$2"; shift 2 ;;
    --ca-cert) CA_CERT="$2"; shift 2 ;;
    --domain-suffix-match) DOMAIN="$2"; shift 2 ;;
    --country) COUNTRY="$2"; shift 2 ;;
    --hidden) HIDDEN="yes"; shift ;;
    --priority) PRIORITY="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "不明な引数: $1" >&2; usage ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "sudo で実行してください" >&2; exit 1; }
[ -n "$SSID" ] || usage
command -v nmcli >/dev/null || { echo "NetworkManager (nmcli) が必要です（Raspberry Pi OS Bookworm 以降は標準）" >&2; exit 1; }

if [ -z "$PASSWORD" ]; then
  if [ -t 0 ]; then
    read -r -s -p "パスワード: " PASSWORD; echo
  else
    IFS= read -r PASSWORD || true
  fi
fi
[ -n "$PASSWORD" ] || { echo "パスワードが空です" >&2; exit 1; }

# Wi-Fi の国コード（未設定だと 5GHz 帯などが使えず接続できないことがある）
if command -v raspi-config >/dev/null; then
  raspi-config nonint do_wifi_country "$COUNTRY" || true
else
  iw reg set "$COUNTRY" || true
fi
rfkill unblock wifi || true

nmcli connection delete "$NAME" >/dev/null 2>&1 || true

common=(
  connection.autoconnect yes
  connection.autoconnect-priority "$PRIORITY"
  connection.autoconnect-retries 0          # 0 = 無制限に再試行
  802-11-wireless.hidden "$HIDDEN"
  802-11-wireless.powersave 2               # 省電力を無効化（切断・遅延の主な原因）
  802-11-wireless.cloned-mac-address permanent   # MAC 登録制のネットワーク向けに実 MAC を使う
  ipv4.method auto
  ipv6.method auto
)

if [ -z "$EAP" ]; then
  nmcli connection add type wifi ifname wlan0 con-name "$NAME" ssid "$SSID" \
    "${common[@]}" \
    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASSWORD"
else
  [ -n "$IDENTITY" ] || { echo "--identity が必要です" >&2; exit 1; }
  eap=(
    wifi-sec.key-mgmt wpa-eap
    802-1x.eap "$EAP"
    802-1x.phase2-auth "$PHASE2"
    802-1x.identity "$IDENTITY"
    802-1x.password "$PASSWORD"
  )
  [ -n "$ANON" ] && eap+=(802-1x.anonymous-identity "$ANON")
  if [ -n "$CA_CERT" ]; then
    eap+=(802-1x.ca-cert "$CA_CERT")
  else
    echo "警告: --ca-cert 未指定のためサーバー証明書を検証しません。可能なら大学の指定する CA 証明書を指定してください。" >&2
  fi
  [ -n "$DOMAIN" ] && eap+=(802-1x.domain-suffix-match "$DOMAIN")
  nmcli connection add type wifi ifname wlan0 con-name "$NAME" ssid "$SSID" \
    "${common[@]}" "${eap[@]}"
fi
chmod 600 "/etc/NetworkManager/system-connections/$NAME.nmconnection" 2>/dev/null || true

# 全 Wi-Fi 接続で省電力を無効化
cat > /etc/NetworkManager/conf.d/99-signage-wifi-powersave.conf <<'EOF'
[connection]
wifi.powersave = 2
EOF
systemctl reload NetworkManager || true

echo "接続を試みます..."
if nmcli --wait 60 connection up "$NAME"; then
  echo "接続しました:"
  nmcli -f GENERAL.CONNECTION,IP4.ADDRESS device show wlan0 || true
else
  echo "接続できませんでした。SSID・パスワード・認証方式を確認してください（自動再試行は続きます）" >&2
fi
echo "wlan0 の MAC アドレス（学内で端末登録が必要な場合に使用）: $(cat /sys/class/net/wlan0/address)"
echo "設定ファイル /etc/signage/signage.toml の [network] connection = \"$NAME\" を確認してください"
