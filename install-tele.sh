#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Add-on bot Telegram untuk personal AI gateway.
# Dijalankan di VPS:   bash install-tele.sh
# ---------------------------------------------------------------------------
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if [ -t 1 ]; then
  C_B=$'\033[1m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_R=$'\033[31m'; C_0=$'\033[0m'
else
  C_B=""; C_G=""; C_Y=""; C_R=""; C_0=""
fi

log()  { printf '%s[INFO]%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '%s[PERINGATAN]%s %s\n' "$C_Y" "$C_0" "$*"; }
err()  { printf '%s[ERROR]%s %s\n' "$C_R" "$C_0" "$*" >&2; }

env_val() {
  local key="$1"
  [ -f .env ] || return 0
  sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*\(.*\)\$/\1/p" .env \
    | tail -n1 \
    | tr -d '\r' \
    | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

is_filled() {
  local v
  v="$(env_val "$1")"
  [ -n "$v" ] || return 1
  case "$v" in
    *ganti*|*GANTI*|*isi-*|*ISI-*|*\<*) return 1 ;;
  esac
  return 0
}

printf '\n%s=== Add-on bot Telegram ===%s\n' "$C_B" "$C_0"

# --------------------------- 1. Validasi .env ------------------------------
if [ ! -f .env ]; then
  err "File .env tidak ditemukan. Jalankan dulu: bash install.sh"
  exit 1
fi

MISSING=0
if ! is_filled TELEGRAM_BOT_TOKEN; then
  err "TELEGRAM_BOT_TOKEN belum diisi di .env (dapat dari @BotFather)."
  MISSING=1
fi
if ! is_filled ALLOWED_CHATS; then
  err "ALLOWED_CHATS belum diisi di .env (chat id Anda, pisahkan koma bila lebih dari satu)."
  MISSING=1
fi
if ! is_filled MASTER_KEY; then
  err "MASTER_KEY belum diisi di .env. Jalankan dulu: bash install.sh"
  MISSING=1
fi
if [ "$MISSING" -ne 0 ]; then
  err "Validasi gagal — bot tidak dijalankan."
  exit 1
fi
log "Validasi .env OK (TELEGRAM_BOT_TOKEN + ALLOWED_CHATS terisi)."

# --------------------------- 2. Compose up --------------------------------
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  err "Docker atau plugin 'docker compose' tidak tersedia. Jalankan dulu: bash install.sh"
  exit 1
fi

log "Menjalankan overlay bot Telegram..."
docker compose -f docker-compose.yml -f docker-compose.tele.yml up -d --remove-orphans bot

# --------------------------- 3. Log bot -----------------------------------
printf '\n%s--- 5 baris terakhir log bot ---%s\n' "$C_B" "$C_0"
docker compose -f docker-compose.yml -f docker-compose.tele.yml logs --tail 5 bot

printf '\n%sSelesai.%s Bot aktif. Kirim /help ke bot Anda untuk mulai.\n' "$C_G" "$C_0"
printf 'Pantau log:  docker compose -f docker-compose.yml -f docker-compose.tele.yml logs -f bot\n'
