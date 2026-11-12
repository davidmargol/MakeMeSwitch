#!/usr/bin/env bash
# Remove MakeMeSwitch CLI. Host networking is never modified.
set -euo pipefail

PURGE="no"
PURGE_DATA="no"
FORCE="no"

log()  { printf '\033[1;36m[makemeswitch]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[makemeswitch]\033[0m %s\n' "$*"; }

for arg in "$@"; do
  case "$arg" in
    --purge) PURGE="yes" ;;
    --purge-data) PURGE="yes"; PURGE_DATA="yes" ;;
    --force) FORCE="yes" ;;
    -h|--help)
      echo "Usage: sudo ./uninstall.sh [--purge] [--purge-data] [--force]"
      exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

log "Stopping services"
systemctl disable --now makemeswitch.service 2>/dev/null || true
systemctl disable --now makemeswitch-nicnames.path makemeswitch-nicnames.service 2>/dev/null || true
systemctl disable --now makemeswitch-snmpd.service 2>/dev/null || true
systemctl disable --now makemeswitch-ssh.service 2>/dev/null || true
rm -f /etc/systemd/system/makemeswitch.service /etc/systemd/system/makemeswitch-nicnames.service \
      /etc/systemd/system/makemeswitch-nicnames.path \
      /etc/systemd/system/makemeswitch-snmpd.service /etc/systemd/system/makemeswitch-ssh.service
rm -f /etc/systemd/network/10-makemeswitch-*.link
udevadm control --reload 2>/dev/null || true
systemctl daemon-reload

# Give SSH back to the host: the switch CLI owned port 22, sshd was on 8022.
if [[ -f /etc/ssh/sshd_config.d/99-makemeswitch.conf ]]; then
  log "Restoring host SSH to port 22"
  rm -f /etc/ssh/sshd_config.d/99-makemeswitch.conf
  if [[ -f /etc/makemeswitch/backup/sshd_config.orig ]]; then
    cp -a /etc/makemeswitch/backup/sshd_config.orig /etc/ssh/sshd_config
  else
    sed -i -E 's/^# Port 22 - moved to 8022 by MakeMeSwitch/Port 22/' /etc/ssh/sshd_config
  fi
  if sshd -t 2>/dev/null; then
    systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
    log "Host SSH is back on port 22"
  else
    warn "sshd rejected the restored configuration - check /etc/ssh/sshd_config by hand"
  fi
fi

if [[ -f /etc/rsyslog.d/50-makemeswitch.conf ]]; then
  log "Removing the rsyslog forwarding rule"
  rm -f /etc/rsyslog.d/50-makemeswitch.conf
  systemctl try-restart rsyslog 2>/dev/null || true
fi

BRIDGE="mms0"
if command -v ovs-vsctl >/dev/null && ovs-vsctl br-exists "$BRIDGE" 2>/dev/null; then
  PORTS="$(ovs-vsctl list-ports "$BRIDGE" 2>/dev/null | tr '\n' ' ')"
  if [[ -n "${PORTS// /}" && "$FORCE" != "yes" ]]; then
    warn "OVS bridge $BRIDGE still has ports: $PORTS"
    warn "Refusing to delete it (this could disconnect the host)."
    warn "Detach ports first, or re-run with --force if you are sure."
  else
    log "Deleting OVS bridge $BRIDGE"
    ovs-vsctl --if-exists del-br "$BRIDGE"
  fi
fi

if [[ "$PURGE" == "yes" ]]; then
  log "Removing program files"
  rm -f /usr/local/bin/makemeswitch
  rm -rf /opt/makemeswitch /etc/makemeswitch /etc/systemd/system/makemeswitch.service
  [[ "$PURGE_DATA" == "yes" ]] && rm -rf /var/lib/makemeswitch /var/log/makemeswitch
  warn "Open vSwitch packages were left installed; remove them manually if unused."
fi

log "Uninstalled. Host network configuration was not touched."
