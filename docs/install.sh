#!/usr/bin/env bash
# MakeMeSwitch one-liner bootstrap installer.
#
# Usage:
#   curl -fsSL https://davidmargol.github.io/MakeMeSwitch/install.sh | sudo bash
#   curl -fsSL https://davidmargol.github.io/MakeMeSwitch/install.sh | sudo bash -s -- --keep-ssh-22 --no-attach-all
#
# Optional environment variables:
#   MMS_REPO     GitHub repository (default: davidmargol/MakeMeSwitch)
#   MMS_VERSION  Release tag/version to install (default: latest)
#   MMS_REF      Git ref (branch/tag) archive to install instead of a release asset
set -euo pipefail

MMS_REPO="${MMS_REPO:-davidmargol/MakeMeSwitch}"
MMS_VERSION="${MMS_VERSION:-latest}"
MMS_REF="${MMS_REF:-}"

log()  { printf '\033[1;36m[makemeswitch]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[makemeswitch]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[makemeswitch]\033[0m %s\n' "$*" >&2; exit 1; }

HELP_ONLY="no"
for arg in "$@"; do
  case "$arg" in
    -h|--help) HELP_ONLY="yes" ;;
  esac
done

if [[ "$HELP_ONLY" != "yes" && $EUID -eq 0 ]]; then
  :
elif [[ "$HELP_ONLY" != "yes" ]]; then
  die "run as root: curl -fsSL https://davidmargol.github.io/MakeMeSwitch/install.sh | sudo bash"
fi

command -v apt-get >/dev/null || die "this installer supports Debian/Ubuntu only"
command -v curl >/dev/null || die "curl is required"
command -v tar >/dev/null || die "tar is required"

resolve_release_asset_from_api() {
  local api_url="$1"
  curl -fsSL "$api_url" 2>/dev/null \
    | grep -Eo '"browser_download_url":[[:space:]]*"[^"]+\.tar\.gz"' \
    | head -n 1 \
    | sed -E 's/.*"([^"]+)".*/\1/' || true
}

ARCHIVE_URL=""
LABEL=""

if [[ -n "$MMS_REF" ]]; then
  ARCHIVE_URL="https://github.com/${MMS_REPO}/archive/${MMS_REF}.tar.gz"
  LABEL="ref ${MMS_REF}"
elif [[ "$MMS_VERSION" == "latest" ]]; then
  latest_url="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/${MMS_REPO}/releases/latest" 2>/dev/null || true)"
  if [[ "$latest_url" == */releases/tag/* ]]; then
    tag="${latest_url##*/}"
    ver="${tag#v}"
    candidate="https://github.com/${MMS_REPO}/releases/download/${tag}/makemeswitch-${ver}.tar.gz"
    if curl -fsSLI "$candidate" >/dev/null 2>&1; then
      ARCHIVE_URL="$candidate"
      LABEL="${tag}"
    fi
  fi

  if [[ -z "$ARCHIVE_URL" ]]; then
    ARCHIVE_URL="$(resolve_release_asset_from_api "https://api.github.com/repos/${MMS_REPO}/releases/latest")"
    [[ -n "$ARCHIVE_URL" ]] && LABEL="latest release"
  fi

  if [[ -z "$ARCHIVE_URL" ]]; then
    warn "could not find a release tarball; falling back to main branch archive"
    ARCHIVE_URL="https://github.com/${MMS_REPO}/archive/refs/heads/main.tar.gz"
    LABEL="main"
  fi
else
  tag="v${MMS_VERSION#v}"
  ver="${tag#v}"
  candidate="https://github.com/${MMS_REPO}/releases/download/${tag}/makemeswitch-${ver}.tar.gz"
  if curl -fsSLI "$candidate" >/dev/null 2>&1; then
    ARCHIVE_URL="$candidate"
  else
    ARCHIVE_URL="$(resolve_release_asset_from_api "https://api.github.com/repos/${MMS_REPO}/releases/tags/${tag}")"
  fi
  [[ -n "$ARCHIVE_URL" ]] || die "could not find release archive for ${tag} in ${MMS_REPO}"
  LABEL="${tag}"
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

ARCHIVE_PATH="$TMP_DIR/makemeswitch.tar.gz"
SRC_DIR="$TMP_DIR/src"
mkdir -p "$SRC_DIR"

log "Downloading MakeMeSwitch (${LABEL})..."
curl -fSL "$ARCHIVE_URL" -o "$ARCHIVE_PATH" || die "failed to download $ARCHIVE_URL"

log "Extracting archive..."
tar -xzf "$ARCHIVE_PATH" -C "$SRC_DIR" || die "failed to extract $ARCHIVE_PATH"

INSTALLER="$(find "$SRC_DIR" -maxdepth 3 -name install.sh -type f | head -n 1)"
[[ -n "$INSTALLER" ]] || die "install.sh not found in downloaded archive"

INSTALLER_DIR="$(dirname "$INSTALLER")"
log "Running MakeMeSwitch installer..."
(cd "$INSTALLER_DIR" && bash ./install.sh "$@")
