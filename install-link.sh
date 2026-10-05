#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Add-on bot pemeriksa tautan (link inspector) untuk personal AI gateway.
# Dijalankan di VPS:   bash install-link.sh
#
# Bot ini membalas kartu pratinjau untuk tautan apa pun (judul, penulis,
# deskripsi, gambar, embed resmi) dan melacak pengalihan shortlink.
# Bot TIDAK mengekstrak stream video, tidak memproses HLS, dan tidak mengunduh
# berkas media.
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

printf '\n%s=== Add-on bot pemeriksa tautan ===%s\n' "$C_B" "$C_0"

# --------------------------- 1. Validasi .env ------------------------------
if [ ! -f .env ]; then
  err "File .env tidak ditemukan. Salin dulu: cp .env.example .env, lalu isi nilainya."
  exit 1
fi

MISSING=0
if ! is_filled LINKBOT_TOKEN && ! is_filled TELEGRAM_BOT_TOKEN; then
  err "Belum ada token: isi LINKBOT_TOKEN (atau TELEGRAM_BOT_TOKEN) dari @BotFather."
  MISSING=1
fi
if ! is_filled ALLOWED_CHATS; then
  err "ALLOWED_CHATS belum diisi di .env (chat id Anda, pisahkan koma bila lebih dari satu)."
  MISSING=1
fi
if [ "$MISSING" -ne 0 ]; then
  err "Validasi gagal — bot tidak dijalankan."
  exit 1
fi
log "Validasi .env OK (token + ALLOWED_CHATS terisi)."

if is_filled LINKBOT_TOKEN && is_filled TELEGRAM_BOT_TOKEN \
   && [ "$(env_val LINKBOT_TOKEN)" = "$(env_val TELEGRAM_BOT_TOKEN)" ]; then
  warn "LINKBOT_TOKEN sama dengan TELEGRAM_BOT_TOKEN — satu bot dipakai dua proses."
  warn "Telegram hanya mengizinkan satu getUpdates per token; salah satu akan error 409."
  warn "Sebaiknya buat bot kedua di @BotFather dan isi LINKBOT_TOKEN."
fi

# --------------------------- 2. Uji mandiri --------------------------------
if command -v python3 >/dev/null 2>&1; then
  log "Menjalankan uji mandiri offline (link-tools/selftest.py)..."
  if (cd link-tools && python3 selftest.py >/dev/null 2>&1); then
    log "Uji mandiri lolos."
  else
    warn "Uji mandiri tidak lolos. Periksa manual: cd link-tools && python3 selftest.py"
  fi
else
  warn "python3 tidak ada di host; uji mandiri dilewati (tetap dijalankan di dalam container)."
fi

# --------------------------- 3. Compose up --------------------------------
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  err "Docker atau plugin 'docker compose' tidak tersedia."
  exit 1
fi

log "Menjalankan overlay bot pemeriksa tautan..."
docker compose -f docker-compose.yml -f docker-compose.link.yml up -d --build linkbot

# --------------------------- 4. Log bot -----------------------------------
printf '\n%s--- 5 baris terakhir log bot ---%s\n' "$C_B" "$C_0"
docker compose -f docker-compose.yml -f docker-compose.link.yml logs --tail 5 linkbot

printf '\n%sSelesai.%s Kirim sebuah tautan ke bot Anda untuk mencoba.\n' "$C_G" "$C_0"
printf 'Pantau log:  docker compose -f docker-compose.yml -f docker-compose.link.yml logs -f linkbot\n'
