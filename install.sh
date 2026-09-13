#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Installer personal AI gateway (LiteLLM proxy + Redis).
# Dijalankan di VPS:   bash install.sh
#
# Catatan: skrip ini TIDAK perlu dijalankan sebagai root bila Docker sudah ada.
# Bila Docker belum ada, skrip butuh sudo/root untuk menginstalnya.
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
head1() { printf '\n%s=== %s ===%s\n' "$C_B" "$*" "$C_0"; }

need_cmd() { command -v "$1" >/dev/null 2>&1; }

# ----------------------------- helper .env ---------------------------------
env_val() {
  local key="$1"
  [ -f .env ] || return 0
  sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*\(.*\)\$/\1/p" .env \
    | tail -n1 \
    | tr -d '\r' \
    | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

set_env_key() {
  local key="$1" val="$2"
  if grep -qE "^[[:space:]]*${key}[[:space:]]*=" .env 2>/dev/null; then
    sed -i "s|^[[:space:]]*${key}[[:space:]]*=.*|${key}=${val}|" .env
  else
    printf '%s=%s\n' "$key" "$val" >> .env
  fi
}

# Dianggap terisi bila tidak kosong dan bukan placeholder.
is_filled() {
  local v
  v="$(env_val "$1")"
  [ -n "$v" ] || return 1
  case "$v" in
    *ganti*|*GANTI*|*isi-*|*ISI-*|*\<*) return 1 ;;
  esac
  return 0
}

# --------------------------- 1. Docker engine ------------------------------
install_docker() {
  if need_cmd docker && docker compose version >/dev/null 2>&1; then
    log "Docker $(docker --version | awk '{print $3}' | tr -d ',') + compose plugin sudah terpasang."
    return 0
  fi

  log "Docker belum lengkap. Menginstal Docker Engine + compose plugin..."

  local sudo=""
  if [ "$(id -u)" -ne 0 ]; then
    if need_cmd sudo; then sudo="sudo"; else
      err "Butuh akses root atau sudo untuk menginstal Docker."
      exit 1
    fi
  fi

  $sudo apt-get update -y
  $sudo apt-get install -y ca-certificates curl gnupg

  if ! need_cmd docker; then
    curl -fsSL https://get.docker.com | $sudo sh
  fi

  if ! docker compose version >/dev/null 2>&1; then
    $sudo apt-get install -y docker-compose-plugin || true
  fi

  $sudo systemctl enable --now docker >/dev/null 2>&1 || true

  if ! need_cmd docker; then
    err "Instalasi Docker gagal. Pasang Docker secara manual lalu jalankan ulang skrip ini."
    exit 1
  fi
  if ! docker compose version >/dev/null 2>&1; then
    err "Plugin 'docker compose' tidak tersedia. Pasang docker-compose-plugin lalu jalankan ulang."
    exit 1
  fi
  log "Docker + compose plugin siap."
}

# --------------------------- 2. File .env ---------------------------------
prepare_env() {
  if [ ! -f .env ]; then
    if [ ! -f .env.example ]; then
      err "File .env.example tidak ditemukan. Jalankan skrip dari dalam folder project."
      exit 1
    fi
    cp .env.example .env
    chmod 600 .env

    local mk
    if need_cmd openssl; then
      mk="$(openssl rand -hex 32)"        # 32 byte acak = 64 karakter hex
    elif [ -r /dev/urandom ]; then
      mk="$(tr -dc 'a-f0-9' < /dev/urandom | head -c 64)"
    else
      err "Tidak bisa membuat MASTER_KEY acak (openssl/urandom tidak tersedia)."
      exit 1
    fi
    set_env_key MASTER_KEY "$mk"

    head1 "Langkah 1/2 — file .env dibuat"
    cat <<EOF
File .env baru saja dibuat dari .env.example dan MASTER_KEY sudah diisi acak.

Sekarang isi kunci gratis Anda di file .env:
    GEMINI_KEY        -> https://aistudio.google.com/apikey
    OPENROUTER_KEY    -> https://openrouter.ai/keys
    AIHUBMIX_KEY      -> https://aihubmix.com
    (opsional, untuk bot Telegram)
    TELEGRAM_BOT_TOKEN-> dari @BotFather
    ALLOWED_CHATS     -> chat id Anda, pisahkan koma bila lebih dari satu

Buka dengan:   nano .env
Setelah selesai, jalankan ULANG:   bash install.sh
EOF
    exit 0
  fi

  chmod 600 .env 2>/dev/null || true

  if ! is_filled MASTER_KEY; then
    local mk
    mk="$(openssl rand -hex 32 2>/dev/null || tr -dc 'a-f0-9' < /dev/urandom | head -c 64)"
    set_env_key MASTER_KEY "$mk"
    log "MASTER_KEY masih kosong -> sudah diisi acak."
  fi
}

warn_empty_keys() {
  local k
  for k in GEMINI_KEY OPENROUTER_KEY AIHUBMIX_KEY; do
    if ! is_filled "$k"; then
      warn "$k masih kosong — alias model yang memakainya akan gagal dipanggil."
    fi
  done
}

# --------------------------- 3. Compose up --------------------------------
compose_up() {
  local -a files=(-f docker-compose.yml)
  local tele_on=0

  if is_filled TELEGRAM_BOT_TOKEN && is_filled ALLOWED_CHATS; then
    files+=(-f docker-compose.tele.yml)
    tele_on=1
    log "TELEGRAM_BOT_TOKEN + ALLOWED_CHATS terisi -> overlay bot Telegram ikut dijalankan."
  else
    warn "TELEGRAM_BOT_TOKEN/ALLOWED_CHATS belum terisi -> bot Telegram dilewati."
    warn "  (setelah diisi, jalankan: bash install-tele.sh)"
  fi

  docker compose "${files[@]}" up -d --remove-orphans

  if [ "$tele_on" -eq 1 ]; then
    log "Container proxy + redis + bot dijalankan."
  else
    log "Container proxy + redis dijalankan."
  fi
}

# --------------------------- 4. Health check ------------------------------
health_check() {
  local url="http://127.0.0.1:4000/health/liveliness"
  local i body=""

  if ! need_cmd curl; then
    warn "curl tidak ditemukan — health check dilewati."
    return 0
  fi

  for i in $(seq 1 30); do
    if body="$(curl -fsS --max-time 5 "$url" 2>/dev/null)"; then
      log "Health check OK (percobaan $i): $body"
      return 0
    fi
    sleep 2
  done

  warn "Health check belum OK setelah 30 percobaan."
  warn "  Lihat log:  docker compose logs --tail=50 proxy"
  return 0
}

# --------------------------- 5. Ringkasan ---------------------------------
print_summary() {
  local mk ip
  mk="$(env_val MASTER_KEY)"
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$ip" ] || ip="IP-VPS"

  head1 "Selesai"
  printf '%sMASTER_KEY (dicatat SEKARANG, tidak ditampilkan lagi):%s\n' "$C_B" "$C_0"
  printf '    %s\n\n' "$mk"

  cat <<EOF
Cara akses dari komputer Anda (SSH tunnel):
    ssh -N -L 4000:127.0.0.1:4000 user@$ip

Lalu buka di browser:
    http://127.0.0.1:4000/ui

Uji cepat lewat tunnel (di komputer Anda):
    curl http://127.0.0.1:4000/v1/chat/completions \\
      -H "Authorization: Bearer $mk" \\
      -H "Content-Type: application/json" \\
      -d '{"model":"pribadi-pro","messages":[{"role":"user","content":"halo"}]}'

Alias model yang tersedia:
    pribadi-pro     (Gemini 2.5 Flash)
    pribadi-jenius  (Gemini 2.5 Pro)
    pribadi-hemat   (OpenRouter xiaomi/mimo-v2-flash:free)
    pribadi-hemat-2 (AIHubMix xiaomi-mimo-v2.5-free)

Perintah berguna:
    docker compose logs -f proxy      # log proxy
    docker compose ps                 # status container
    docker compose down               # hentikan semua
EOF
}

main() {
  head1 "Personal AI gateway — installer"
  install_docker
  prepare_env
  warn_empty_keys
  compose_up
  health_check
  print_summary
}

main "$@"
