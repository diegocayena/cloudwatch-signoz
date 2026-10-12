#!/usr/bin/env bash
# Run on the EC2 HOST, not inside the collector container.
set -euo pipefail
export LC_ALL=C

if [[ ${EUID} -ne 0 ]]; then
  echo 'Execute: sudo bash scripts/fix-ec2-clock.sh' >&2
  exit 1
fi
if [[ -f /.dockerenv ]] || ! command -v systemctl >/dev/null; then
  echo 'Execute no host EC2 Linux com systemd, fora do container.' >&2
  exit 1
fi

if command -v apt-get >/dev/null; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y chrony
  config=/etc/chrony/chrony.conf
  service=chrony
elif command -v dnf >/dev/null; then
  dnf install -y chrony
  config=/etc/chrony.conf
  service=chronyd
elif command -v yum >/dev/null; then
  yum install -y chrony
  config=/etc/chrony.conf
  service=chronyd
else
  echo 'Distribuicao nao suportada: configure chrony conforme docs/request-expired.md.' >&2
  exit 1
fi

# Keep the original configuration and avoid duplicate managed entries on reruns.
backup="${config}.before-cloudwatch-signoz"
if [[ ! -e "$backup" ]]; then
  cp -p "$config" "$backup"
fi
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
{
  echo '# BEGIN cloudwatch-signoz Amazon Time Sync'
  echo 'server 169.254.169.123 prefer iburst minpoll 4 maxpoll 4'
  echo '# END cloudwatch-signoz Amazon Time Sync'
  sed '/^# BEGIN cloudwatch-signoz Amazon Time Sync$/,/^# END cloudwatch-signoz Amazon Time Sync$/d' "$config"
} > "$tmp"
# Validate before replacing the configuration or restarting the daemon.
chronyd -p -f "$tmp" >/dev/null
cat "$tmp" > "$config"

# Avoid two daemons competing to discipline the same host clock.
for other in systemd-timesyncd ntp ntpd; do
  if systemctl is-active --quiet "$other"; then
    systemctl disable --now "$other"
  fi
done
systemctl enable "$service"
systemctl restart "$service"
chronyc online
chronyc burst 4/4
# Wait for a usable reference before stepping; then require <100ms correction.
chronyc waitsync 30 0 0 1
chronyc makestep
chronyc waitsync 30 0.1 0 1
chronyc sources -n
chronyc tracking
if ! chronyc sources -n | awk '$1 == "^*" && $2 == "169.254.169.123" {found=1} END {exit !found}'; then
  echo 'Amazon Time Sync nao foi selecionado. Verifique UDP 123 e a configuracao chrony.' >&2
  exit 1
fi
date -u
echo "Relogio sincronizado; servico ${service} habilitado no boot. Backup: ${backup}"
