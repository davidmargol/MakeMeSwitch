#!/usr/bin/env bash
# MakeMeSwitch CLI installer for Debian 12/13 and Ubuntu 22.04/24.04+.
#
# The installer never touches existing host networking: it does not create
# bridges with ports, does not change routes, and does not disable SSH.
set -euo pipefail

APP_DIR="/opt/makemeswitch/app"
VENV_DIR="/opt/makemeswitch/venv"
ETC_DIR="/etc/makemeswitch"
STATE_DIR="/var/lib/makemeswitch"
LOG_DIR="/var/log/makemeswitch"
SERVICE="/etc/systemd/system/makemeswitch.service"
BIND="0.0.0.0"
PORT="8000"
ENABLE_WEB="yes"
USE_HTTPS="no"
TLS_CERT=""
TLS_KEY=""
ADMIN_USER="admin"
INIT_PW="${MMS_INIT_PW:-MakeMeSwitch}"
WITH_LLDP="yes"
ATTACH_ALL="yes"
ASSUME_YES="no"
SSH_SWITCH="yes"

log()  { printf '\033[1;36m[makemeswitch]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[makemeswitch]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[makemeswitch]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: sudo ./install.sh [options]

  --no-web                 install the CLI/API only, disable the web panel
  --bind ADDR              API/panel bind address (default 0.0.0.0 = LAN)
  --port PORT              API/panel port (default 8000)
  --https                  serve the panel over HTTPS
  --tls-cert PATH          TLS certificate (PEM)
  --tls-key PATH           TLS private key (PEM)
  --admin-user NAME        initial administrator (default admin)
  --admin-password PASS    initial password (default: MakeMeSwitch)
  --with-lldp              install lldpd for neighbour discovery (default: on)
  --no-attach-all          leave physical NICs unassigned (default: attach all)
  --keep-ssh-22            do not move host SSH to 8022 (switch CLI stays off port 22)
  -y, --yes                non-interactive
  -h, --help               this help

Example:
  sudo ./install.sh --bind 0.0.0.0 --https \\
      --tls-cert /etc/ssl/lab.crt --tls-key /etc/ssl/lab.key
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-web) ENABLE_WEB="no" ;;
    --bind) BIND="$2"; shift ;;
    --port) PORT="$2"; shift ;;
    --https) USE_HTTPS="yes" ;;
    --tls-cert) TLS_CERT="$2"; shift ;;
    --tls-key) TLS_KEY="$2"; shift ;;
    --admin-user) ADMIN_USER="$2"; shift ;;
    --admin-password) INIT_PW="$2"; shift ;;
    --with-lldp) WITH_LLDP="yes" ;;
    --no-attach-all) ATTACH_ALL="no" ;;
    --keep-ssh-22) SSH_SWITCH="no" ;;
    -y|--yes) ASSUME_YES="yes" ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
  shift
done

[[ $EUID -eq 0 ]] || die "run as root: sudo ./install.sh"
command -v apt-get >/dev/null || die "this installer supports Debian/Ubuntu only"

# --- OS detection ----------------------------------------------------------
. /etc/os-release
case "${ID:-}" in
  debian) MIN_VERSION=12 ;;
  ubuntu) MIN_VERSION=22.04 ;;
  *) die "unsupported distribution '${ID:-unknown}' (Debian 12+ or Ubuntu 22.04+ required)" ;;
esac
VERSION_ID="${VERSION_ID:-0}"
if [[ "$(printf '%s\n%s\n' "$MIN_VERSION" "$VERSION_ID" | sort -V | head -1)" != "$MIN_VERSION" ]]; then
  warn "tested on ${ID} ${MIN_VERSION}+, this system is ${VERSION_ID}; continuing"
fi
log "Installing MakeMeSwitch CLI on ${PRETTY_NAME:-$ID $VERSION_ID}"

# --- dependencies ----------------------------------------------------------
log "Installing packages (Open vSwitch, Python, iproute2)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
PACKAGES=(openvswitch-switch python3 python3-venv python3-pip iproute2 ethtool openssl curl ca-certificates frr)
[[ "$WITH_LLDP" == "yes" ]] && PACKAGES+=(lldpd)
apt-get install -y -qq "${PACKAGES[@]}" >/dev/null
systemctl enable --now lldpd frr
systemctl is-active --quiet lldpd || die "lldpd failed to start"
systemctl is-active --quiet frr || die "FRRouting failed to start"

systemctl enable --now openvswitch-switch >/dev/null 2>&1 || true
ovs-vsctl --version >/dev/null || die "Open vSwitch failed to start"

# --- directories -----------------------------------------------------------
install -d -m 0755 /opt/makemeswitch "$APP_DIR"
install -d -m 0750 "$ETC_DIR" "$STATE_DIR" "$LOG_DIR"

log "Copying application to $APP_DIR"
if command -v rsync >/dev/null; then
  rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude 'node_modules' --exclude 'frontend/node_modules' \
    --exclude '__pycache__' --exclude 'tests' --exclude '*.pyc' \
    "$(cd "$(dirname "$0")" && pwd)/" "$APP_DIR/"
else
  cp -a "$(cd "$(dirname "$0")" && pwd)/." "$APP_DIR/"
  rm -rf "$APP_DIR/.git" "$APP_DIR/.venv" "$APP_DIR/frontend/node_modules" "$APP_DIR/frontend/node_modules" 2>/dev/null || true
fi
chown -R root:root /opt/makemeswitch

# --- python environment ----------------------------------------------------
log "Creating isolated Python environment"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --no-cache-dir --upgrade pip wheel
"$VENV_DIR/bin/pip" install --quiet --no-cache-dir "$APP_DIR"

# Make the CLI reachable as plain `makemeswitch` for every user.
log "Linking the CLI into /usr/local/bin"
ln -sf "$VENV_DIR/bin/makemeswitch" /usr/local/bin/makemeswitch
[[ -x /usr/local/bin/makemeswitch ]] || die "could not install /usr/local/bin/makemeswitch"

# --- frontend --------------------------------------------------------------
if [[ "$ENABLE_WEB" == "yes" && ! -f "$APP_DIR/makemeswitch/web/dist/index.html" ]]; then
  log "Web assets not shipped in this source tree; building the panel"
  if ! command -v npm >/dev/null; then
    log "Installing Node.js and npm (required to build the panel)"
    apt-get install -y -qq nodejs npm >/dev/null || die "could not install nodejs/npm (the panel cannot be built)"
  fi
  if ! (cd "$APP_DIR/frontend" && npm ci --no-audit --no-fund >/dev/null && npm run build); then
    die "web panel build failed - the installer must not finish without a working UI"
  fi
fi

if [[ "$ENABLE_WEB" == "yes" && ! -f "$APP_DIR/makemeswitch/web/dist/index.html" ]]; then
  die "web assets are still missing after the build step ($APP_DIR/makemeswitch/web/dist/index.html)"
fi

# --- configuration ---------------------------------------------------------
if [[ ! -f "$ETC_DIR/makemeswitch.yaml" ]]; then
  log "Writing $ETC_DIR/makemeswitch.yaml"
  cat > "$ETC_DIR/makemeswitch.yaml" <<EOF
bridge: mms0
db_path: $STATE_DIR/makemeswitch.db
state_dir: $STATE_DIR
log_dir: $LOG_DIR
allow_virtual_ports: false
# NICs added later (more adapters in a VM, USB dongles) join the switch
# automatically. Set to false to attach ports by hand.
attach_new_nics: true
stp_mode: rstp
stp_priority: 32768
lldp_enabled: $([[ "$WITH_LLDP" == "yes" ]] && echo true || echo false)

api:
  enabled: true
  bind: $BIND
  port: $PORT
  https: $([[ "$USE_HTTPS" == "yes" ]] && echo true || echo false)
  certfile: ${TLS_CERT:-$ETC_DIR/tls/cert.pem}
  keyfile: ${TLS_KEY:-$ETC_DIR/tls/key.pem}
  session_ttl_seconds: 28800
  login_rate_limit: 10
  allowed_origins: []

web:
  enabled: $([[ "$ENABLE_WEB" == "yes" ]] && echo true || echo false)

cli:
  history_file: $STATE_DIR/cli_history
  idle_timeout: 900
EOF
  chmod 640 "$ETC_DIR/makemeswitch.yaml"
else
  log "Keeping existing $ETC_DIR/makemeswitch.yaml"
fi

if [[ "$USE_HTTPS" == "yes" && ! -f "$ETC_DIR/tls/cert.pem" ]]; then
  install -d -m 0750 "$ETC_DIR/tls"
  log "Generating a self-signed certificate (replace it for production)"
  openssl req -x509 -newkey rsa:3072 -nodes -days 825 \
    -keyout "$ETC_DIR/tls/key.pem" -out "$ETC_DIR/tls/cert.pem" \
    -subj "/CN=makemeswitch.local" >/dev/null 2>&1
  chmod 600 "$ETC_DIR/tls/key.pem"
fi

# Explicit boot naming after OVS is ready. Disable the old reentrant sysfs
# watcher and retire stale .link rules; neither is a reliable hotplug contract.
systemctl disable --now makemeswitch-nicnames.path 2>/dev/null || true
systemctl stop makemeswitch.service 2>/dev/null || true
rm -f /etc/systemd/system/makemeswitch-nicnames.path
rm -f /etc/systemd/network/10-makemeswitch-*.link
udevadm control --reload 2>/dev/null || true
cat > /etc/systemd/system/makemeswitch-nicnames.service <<UNIT
[Unit]
Description=MakeMeSwitch boot NIC discovery and naming
Wants=systemd-udev-settle.service network-online.target
Requires=openvswitch-switch.service
After=systemd-udev-settle.service local-fs.target openvswitch-switch.service network-online.target
Before=makemeswitch.service

[Service]
Type=oneshot
TimeoutStartSec=300
Environment=PYTHONUNBUFFERED=1
Environment=MMS_CONFIG=$ETC_DIR/makemeswitch.yaml
ExecStart=$VENV_DIR/bin/python -m makemeswitch.nic_names apply
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable makemeswitch-nicnames.service >/dev/null
systemctl restart makemeswitch-nicnames.service || die "NIC naming failed; inspect journalctl -u makemeswitch-nicnames"

# --- systemd ---------------------------------------------------------------
log "Installing systemd service"
cat > "$SERVICE" <<EOF
[Unit]
Description=MakeMeSwitch CLI and web management panel
Documentation=file://$APP_DIR/README.md
After=openvswitch-switch.service network-online.target makemeswitch-nicnames.service
# Naming is important but must not gate the panel: if Open vSwitch is broken the
# naming unit fails, and an operator still needs the UI/CLI to diagnose it.
Wants=openvswitch-switch.service network-online.target makemeswitch-nicnames.service

[Service]
Type=simple
Environment=MMS_CONFIG=$ETC_DIR/makemeswitch.yaml
Environment=MMS_APP_DIR=$APP_DIR
Environment=MMS_WEB_DIST=$APP_DIR/makemeswitch/web/dist
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_DIR/bin/python -m makemeswitch.main serve
WorkingDirectory=$APP_DIR
Restart=on-failure
RestartSec=3
User=root
# Privileges: netlink/OVS control only, no new privileges, read-only /usr.
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE CAP_DAC_OVERRIDE
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE CAP_DAC_OVERRIDE
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$STATE_DIR $LOG_DIR /run/openvswitch /run/lock
RuntimeDirectory=makemeswitch

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable makemeswitch.service >/dev/null

# --- administrator ---------------------------------------------------------
# Created BEFORE the first start: otherwise the service generates its own random
# password and the documented default would never work.
CRED_FILE="/root/makemeswitch-initial-credentials.txt"
CREATED_ADMIN="no"
[[ -z "$INIT_PW" ]] && INIT_PW="MakeMeSwitch"
export MMS_CONFIG="$ETC_DIR/makemeswitch.yaml"

if [[ "$(sudo -u root "$VENV_DIR/bin/python" -m makemeswitch.main user list 2>/dev/null | grep -c . || true)" == "0" ]]; then
  CREATED_ADMIN="created"
  "$VENV_DIR/bin/python" -m makemeswitch.main user add "$ADMIN_USER" --role admin --password "$INIT_PW" >/dev/null
else
  # Deterministic post-install credentials: the requested account always ends up
  # with the documented password unless --admin-password changed it.
  CREATED_ADMIN="updated"
  if ! "$VENV_DIR/bin/python" -m makemeswitch.main user passwd "$ADMIN_USER" --password "$INIT_PW" >/dev/null 2>&1; then
    "$VENV_DIR/bin/python" -m makemeswitch.main user add "$ADMIN_USER" --role admin --password "$INIT_PW" >/dev/null
    CREATED_ADMIN="created"
  fi
fi
umask 077
cat > "$CRED_FILE" <<EOF
MakeMeSwitch administrator
username: $ADMIN_USER
password: $INIT_PW
Change it with: sudo makemeswitch user passwd $ADMIN_USER
EOF
chmod 600 "$CRED_FILE"
log "Administrator '$ADMIN_USER' ready (password: $INIT_PW) - also saved in $CRED_FILE"
[[ "$INIT_PW" == "MakeMeSwitch" ]] && warn "Default password in use - change it: sudo makemeswitch user passwd $ADMIN_USER"

systemctl restart makemeswitch.service

# --- switch ports -----------------------------------------------------------
if [[ "$ATTACH_ALL" == "yes" ]]; then
  log "Adding every physical NIC (except management) to the switch"
  MMS_CONFIG="$ETC_DIR/makemeswitch.yaml" "$VENV_DIR/bin/python" -m makemeswitch.bootstrap_attach \
    || die "could not attach the physical ports (see: journalctl -u makemeswitch -n 50)"
  systemctl restart makemeswitch.service
  sleep 2
else
  log "Leaving physical NICs unassigned (--no-attach-all)"
fi

# --- SNMP agent (read-only, v2c) -------------------------------------------
log "Installing the SNMPv2c agent"
if ss -ltnu 2>/dev/null | grep -qE ':161[[:space:]]' && ! systemctl is-active --quiet makemeswitch-snmpd 2>/dev/null; then
  warn "port 161/udp is already in use (net-snmp?); the MakeMeSwitch agent stays disabled"
  SNMP_OK="no"
else
  SNMP_OK="yes"
fi
cat > /etc/systemd/system/makemeswitch-snmpd.service <<UNIT
[Unit]
Description=MakeMeSwitch SNMPv2c agent (read-only switch statistics)
After=network-online.target makemeswitch.service
Wants=network-online.target makemeswitch.service

[Service]
Type=simple
Environment=MMS_CONFIG=$ETC_DIR/makemeswitch.yaml
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_DIR/bin/python -m makemeswitch.core.snmp
WorkingDirectory=$APP_DIR
Restart=on-failure
RestartSec=5
User=root
# Only the privileged bind needs a capability; everything else is read-only.
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadOnlyPaths=/sys/class/net
# ovs-appctl needs read-write access to the vswitchd pid/socket to dump the
# forwarding table (BRIDGE-MIB dot1dTpFdbTable); without it every FDB walk is empty.
ReadWritePaths=$STATE_DIR /run/openvswitch

[Install]
WantedBy=multi-user.target
UNIT

# --- remote syslog ----------------------------------------------------------
RSYSLOG_UNIT="/etc/rsyslog.d/50-makemeswitch.conf"
if command -v rsyslogd >/dev/null 2>&1; then
  log "rsyslog present: remote logging will be configured from the switch config"
else
  warn "rsyslog is not installed; remote logging from the host will not be forwarded (the switch still sends its own events)"
fi

# --- SSH: switch CLI on 22, host administration on 8022 ---------------------
if [[ "$SSH_SWITCH" == "yes" ]]; then
  log "Putting the switch CLI on SSH port 22 and moving host SSH to 8022"
  install -d -m 750 "$ETC_DIR/backup"
  if [[ ! -f "$ETC_DIR/backup/sshd_config.orig" ]]; then
    cp -a /etc/ssh/sshd_config "$ETC_DIR/backup/sshd_config.orig"
  fi
  install -d -m 755 /etc/ssh/sshd_config.d
  cat > /etc/ssh/sshd_config.d/99-makemeswitch.conf <<'SSHDCONF'
# Managed by MakeMeSwitch: the appliance CLI owns port 22, host SSH keeps 8022.
Port 8022
SSHDCONF
  # sshd cannot remove a port from a drop-in, so neutralise the original line.
  sed -i -E 's/^[[:space:]]*Port[[:space:]]+22[[:space:]]*$/# Port 22 - moved to 8022 by MakeMeSwitch/' /etc/ssh/sshd_config
  if ! sshd -t 2>/dev/null; then
    warn "sshd rejected the new configuration; restoring the original"
    cp -a "$ETC_DIR/backup/sshd_config.orig" /etc/ssh/sshd_config
    rm -f /etc/ssh/sshd_config.d/99-makemeswitch.conf
    die "sshd configuration restored - host SSH is still on port 22"
  fi
  systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
  for _ in $(seq 1 15); do
    ss -ltn 2>/dev/null | grep -qE ':8022[[:space:]]' && break
    sleep 1
  done
  if ! ss -ltn 2>/dev/null | grep -qE ':8022[[:space:]]'; then
    cp -a "$ETC_DIR/backup/sshd_config.orig" /etc/ssh/sshd_config
    rm -f /etc/ssh/sshd_config.d/99-makemeswitch.conf
    systemctl restart ssh 2>/dev/null || true
    die "sshd did not come up on 8022; original configuration restored (SSH on 22)"
  fi
  log "Host SSH is listening on 8022 (existing sessions were not touched)"

  if [[ ! -f "$ETC_DIR/ssh_host_ed25519_key" ]]; then
    ssh-keygen -q -t ed25519 -N '' -C "makemeswitch" -f "$ETC_DIR/ssh_host_ed25519_key"
  fi
  chmod 600 "$ETC_DIR/ssh_host_ed25519_key"

  cat > /etc/systemd/system/makemeswitch-ssh.service <<UNIT
[Unit]
Description=MakeMeSwitch switch CLI over SSH (port 22)
After=network-online.target makemeswitch.service
Wants=network-online.target makemeswitch.service

[Service]
Type=simple
Environment=MMS_CONFIG=$ETC_DIR/makemeswitch.yaml
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_DIR/bin/python -m makemeswitch.core.ssh_server --host 0.0.0.0 --port 22 --host-key $ETC_DIR/ssh_host_ed25519_key
WorkingDirectory=$APP_DIR
Restart=on-failure
RestartSec=3
User=root
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$STATE_DIR $ETC_DIR

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable makemeswitch-ssh.service >/dev/null 2>&1 || true
  if ! systemctl restart makemeswitch-ssh.service; then
    warn "the switch SSH service failed to start; giving port 22 back to the host sshd"
    systemctl disable --now makemeswitch-ssh.service >/dev/null 2>&1 || true
    cp -a "$ETC_DIR/backup/sshd_config.orig" /etc/ssh/sshd_config
    rm -f /etc/ssh/sshd_config.d/99-makemeswitch.conf
    systemctl restart ssh 2>/dev/null || true
    die "switch SSH could not start; host SSH restored on port 22 (see: journalctl -u makemeswitch-ssh)"
  fi
  for _ in $(seq 1 10); do
    ss -ltn 2>/dev/null | grep -qE ':22[[:space:]]' && break
    sleep 1
  done
  if ! ss -ltn 2>/dev/null | grep -qE ':22[[:space:]]'; then
    warn "nothing is listening on port 22; restoring host SSH there"
    systemctl disable --now makemeswitch-ssh.service >/dev/null 2>&1 || true
    cp -a "$ETC_DIR/backup/sshd_config.orig" /etc/ssh/sshd_config
    rm -f /etc/ssh/sshd_config.d/99-makemeswitch.conf
    systemctl restart ssh 2>/dev/null || true
    die "switch SSH did not bind port 22; host SSH restored on port 22"
  fi
  log "Switch CLI answers on SSH 22 (appliance accounts); host admin SSH on 8022"
else
  warn "Host SSH stays on port 22 (--keep-ssh-22): the switch CLI is console/panel only"
  systemctl disable --now makemeswitch-ssh.service >/dev/null 2>&1 || true
fi

if [[ "$SNMP_OK" == "yes" ]]; then
  systemctl daemon-reload
  systemctl enable makemeswitch-snmpd.service >/dev/null 2>&1 || true
  if systemctl restart makemeswitch-snmpd.service; then
    log "SNMPv2c agent listening on udp/161 (read-only, community 'public' by default)"
  else
    warn "the SNMP agent did not start; check: journalctl -u makemeswitch-snmpd"
  fi
else
  systemctl disable --now makemeswitch-snmpd.service >/dev/null 2>&1 || true
fi

# --- health check ----------------------------------------------------------
SCHEME="http"; [[ "$USE_HTTPS" == "yes" ]] && SCHEME="https"
CHECK_HOST="$BIND"; [[ "$CHECK_HOST" == "0.0.0.0" || "$CHECK_HOST" == "::" ]] && CHECK_HOST="127.0.0.1"
for _ in $(seq 1 20); do
  if curl -ksf "$SCHEME://$CHECK_HOST:$PORT/" >/dev/null 2>&1; then break; fi
  sleep 1
done

# The panel must actually serve the SPA, not the "not built" JSON stub.
if [[ "$ENABLE_WEB" == "yes" ]]; then
  BODY="$(curl -ks "$SCHEME://$CHECK_HOST:$PORT/" || true)"
  if ! grep -qi "MakeMeSwitch Manager" <<<"$BODY"; then
    die "the panel is not being served (got: $(head -c 160 <<<"$BODY")). Check MMS_WEB_DIST and $APP_DIR/makemeswitch/web/dist."
  fi
  # Prove the documented credentials really work, instead of assuming it.
  LOGIN_CODE="$(curl -ks -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
      -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$INIT_PW\"}" \
      "$SCHEME://$CHECK_HOST:$PORT/api/v1/auth/login" || true)"
  if [[ "$LOGIN_CODE" != "200" ]]; then
    warn "login check failed (HTTP $LOGIN_CODE). Reset with: sudo makemeswitch user passwd $ADMIN_USER"
  else
    log "Login verified: $ADMIN_USER / $INIT_PW"
  fi
fi

echo
log "MakeMeSwitch CLI installed."
LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
[[ -z "$LAN_IP" ]] && LAN_IP="<switch-ip>"
PANEL_HOST="$BIND"
if [[ "$BIND" == "0.0.0.0" || "$BIND" == "::" ]]; then PANEL_HOST="$LAN_IP"; fi
echo "  CLI:        sudo makemeswitch"
echo "  Web panel:  $SCHEME://$PANEL_HOST:$PORT/"
echo "  Login:      ${ADMIN_USER} / ${INIT_PW}   (saved in $CRED_FILE)"
echo "  Service:    systemctl status makemeswitch"
if [[ "$SSH_SWITCH" == "yes" ]]; then
  echo "  SSH:        ssh $ADMIN_USER@$LAN_IP        -> switch CLI (appliance accounts)"
  echo "              ssh debian@$LAN_IP -p 8022 -> host shell (OS accounts)"
fi
echo "  SNMP:       udp/161 v2c, community 'public' (read-only)"
echo "  Monitoring: snmpwalk -v2c -c public $LAN_IP 1.3.6.1.2.1.1"
echo "  Config:     $ETC_DIR/makemeswitch.yaml"
echo "  Database:   $STATE_DIR/makemeswitch.db"
echo
if [[ "$ATTACH_ALL" == "yes" ]]; then
  log "All physical NICs except management are switch ports (Ethernet1, Ethernet2, …)."
  echo "   Check them: sudo makemeswitch   ->   show interfaces status"
  echo "   Management NIC is never enslaved."
else
  warn "No physical NIC was enslaved (--no-attach-all). Pick switch ports explicitly:"
  echo "   Switch# configure terminal"
  echo "   Switch(config)# interface ethernet 1"
  echo "   Switch(config-if)# switchport attach enp1s0"
fi
echo
if [[ "$USE_HTTPS" != "yes" ]]; then
  warn "The panel is served over plain HTTP on the LAN. Re-run with --https (and a cert) if the network is not trusted."
fi
