#!/usr/bin/env python3
"""Bot Telegram untuk personal AI gateway (LiteLLM proxy).

Hanya memakai Python standard library — tidak perlu pip install apa pun.
Bot bekerja dengan long-polling getUpdates: tanpa webhook, tanpa port masuk.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque

# --------------------------------------------------------------------------
# Konfigurasi (semua dari environment / file .env lewat docker compose)
# --------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
MASTER_KEY = os.environ.get("MASTER_KEY", "").strip()
LITELLM_URL = os.environ.get("LITELLM_URL", "http://proxy:4000").rstrip("/")
DEFAULT_MODEL = os.environ.get("BOT_MODEL", "pribadi-pro").strip() or "pribadi-pro"

RAW_ALLOWED_CHATS = os.environ.get("ALLOWED_CHATS", "")
ALLOWED_CHATS = {c.strip() for c in RAW_ALLOWED_CHATS.split(",") if c.strip()}

MEMORY_LEN = int(os.environ.get("BOT_MEMORY", "20"))          # 20 pesan terakhir per chat
REQUEST_TIMEOUT = int(os.environ.get("BOT_TIMEOUT", "240"))   # timeout panggil gateway
POLL_TIMEOUT = int(os.environ.get("BOT_POLL_TIMEOUT", "50"))  # long-polling 50 detik
MAX_CHARS = int(os.environ.get("BOT_MAX_CHARS", "4096"))      # batas pesan Telegram
SEND_RETRIES = int(os.environ.get("BOT_SEND_RETRIES", "3"))   # retry kirim 3x
TYPING_INTERVAL = float(os.environ.get("BOT_TYPING_INTERVAL", "4"))
SYSTEM_PROMPT = os.environ.get(
    "BOT_SYSTEM_PROMPT",
    "Anda adalah asisten pribadi yang ringkas, jelas, dan membantu. Jawab dalam bahasa yang dipakai pengguna.",
)

KNOWN_MODELS = ["pribadi-pro", "pribadi-jenius", "pribadi-hemat", "pribadi-hemat-2"]

TELEGRAM_API = "https://api.telegram.org/bot%s" % BOT_TOKEN

logging.basicConfig(
    level=os.environ.get("BOT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("pgw-bot")

# Memori percakapan dan pilihan model per chat.
HISTORY: dict[str, deque] = defaultdict(lambda: deque(maxlen=MEMORY_LEN))
MODELS: dict[str, str] = defaultdict(lambda: DEFAULT_MODEL)

HELP_TEXT = (
    "Personal AI gateway siap dipakai.\n"
    "\n"
    "Perintah:\n"
    "  /model            — tampilkan model yang sedang dipakai\n"
    "  /model <alias>    — ganti model untuk chat ini\n"
    "  /reset            — hapus memori percakapan chat ini\n"
    "  /help             — tampilkan pesan ini\n"
    "\n"
    "Alias model:\n"
    "  pribadi-pro       — Gemini 2.5 Flash (cepat)\n"
    "  pribadi-jenius    — Gemini 2.5 Pro (lebih pintar, kuota ketat)\n"
    "  pribadi-hemat     — OpenRouter xiaomi/mimo-v2-flash:free\n"
    "  pribadi-hemat-2   — AIHubMix xiaomi-mimo-v2.5-free\n"
    "\n"
    "Memori: %d pesan terakhir per chat." % MEMORY_LEN
)


# --------------------------------------------------------------------------
# Utilitas HTTP
# --------------------------------------------------------------------------
def http_json(url: str, payload: dict | None = None, headers: dict | None = None, timeout: int = 60) -> dict:
    """Kirim request JSON dan kembalikan respons sebagai dict."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    return json.loads(body) if body else {}


def tg_call(method: str, payload: dict | None = None, timeout: int = 60) -> dict:
    return http_json("%s/%s" % (TELEGRAM_API, method), payload, timeout=timeout)


def send_message(chat_id: str, text: str) -> None:
    """Kirim pesan dengan retry (maksimal SEND_RETRIES kali)."""
    last_error: Exception | None = None
    for attempt in range(1, SEND_RETRIES + 1):
        try:
            tg_call("sendMessage", {"chat_id": chat_id, "text": text}, timeout=30)
            return
        except Exception as exc:  # noqa: BLE001 - ingin retry pada error apa pun
            last_error = exc
            log.warning("Gagal kirim pesan (percobaan %d/%d): %s", attempt, SEND_RETRIES, exc)
            if attempt < SEND_RETRIES:
                time.sleep(2 * attempt)
    log.error("Gagal kirim pesan setelah %d percobaan: %s", SEND_RETRIES, last_error)


def split_text(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Pecah teks panjang di batas baris agar aman untuk batas Telegram."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buffer = ""
    for line in text.splitlines(keepends=True):
        # Baris tunggal yang lebih panjang dari batas -> potong paksa.
        while len(line) > limit:
            if buffer:
                chunks.append(buffer.rstrip("\n"))
                buffer = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(buffer) + len(line) > limit:
            chunks.append(buffer.rstrip("\n"))
            buffer = line
        else:
            buffer += line
    if buffer:
        chunks.append(buffer.rstrip("\n"))
    return [c for c in chunks if c.strip()]


def send_long_message(chat_id: str, text: str) -> None:
    for chunk in split_text(text):
        send_message(chat_id, chunk)


def start_typing(chat_id: str) -> threading.Event:
    """Kirim chat action 'typing' berulang sampai event di-set."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(TYPING_INTERVAL):
            try:
                tg_call("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=15)
            except Exception as exc:  # noqa: BLE001
                log.debug("Gagal kirim chat action: %s", exc)

    try:
        tg_call("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=15)
    except Exception as exc:  # noqa: BLE001
        log.debug("Gagal kirim chat action pertama: %s", exc)

    threading.Thread(target=loop, name="typing-%s" % chat_id, daemon=True).start()
    return stop


# --------------------------------------------------------------------------
# Pemanggilan gateway
# --------------------------------------------------------------------------
def ask_gateway(messages: list[dict], model: str) -> str:
    headers = {"Authorization": "Bearer %s" % MASTER_KEY} if MASTER_KEY else {}
    payload = {"model": model, "messages": messages}
    try:
        data = http_json(
            "%s/v1/chat/completions" % LITELLM_URL,
            payload,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError("HTTP %s dari gateway. %s" % (exc.code, detail)) from None
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Gagal menghubungi gateway: %s" % exc) from None

    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("Format respons gateway tidak dikenali: %s" % json.dumps(data)[:300]) from None


# --------------------------------------------------------------------------
# Penanganan pesan
# --------------------------------------------------------------------------
def handle_message(message: dict) -> None:
    chat_id = str(message.get("chat", {}).get("id", ""))
    if chat_id not in ALLOWED_CHATS:
        log.info("Pesan dari chat %s diabaikan (tidak ada di ALLOWED_CHATS).", chat_id)
        return

    text = (message.get("text") or "").strip()
    if not text:
        send_message(chat_id, "Maaf, saat ini saya hanya memproses pesan teks.")
        return

    if text.startswith("/"):
        handle_command(chat_id, text)
        return

    history = HISTORY[chat_id]
    model = MODELS[chat_id]

    history.append({"role": "user", "content": text})
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + list(history)

    stop_typing = start_typing(chat_id)
    try:
        reply = ask_gateway(messages, model)
    except Exception as exc:  # noqa: BLE001
        log.error("Error gateway untuk chat %s: %s", chat_id, exc)
        history.pop()  # jangan simpan pertanyaan yang gagal diproses
        send_message(chat_id, "Maaf, gateway sedang bermasalah:\n%s" % exc)
        return
    finally:
        stop_typing.set()

    if not reply.strip():
        reply = "(gateway mengembalikan jawaban kosong)"

    history.append({"role": "assistant", "content": reply})
    send_long_message(chat_id, reply)


def handle_command(chat_id: str, text: str) -> None:
    parts = text.split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""

    if command in ("/help", "/start"):
        send_message(chat_id, HELP_TEXT)
        return

    if command == "/reset":
        HISTORY[chat_id].clear()
        send_message(chat_id, "Memori percakapan dibersihkan.")
        return

    if command == "/model":
        if not argument:
            send_message(
                chat_id,
                "Model aktif: %s\nGanti dengan: /model <alias>\nPilihan: %s"
                % (MODELS[chat_id], ", ".join(KNOWN_MODELS)),
            )
            return
        alias = argument.strip()
        MODELS[chat_id] = alias
        if alias in KNOWN_MODELS:
            send_message(chat_id, "Model diganti ke %s." % alias)
        else:
            send_message(
                chat_id,
                "Model diganti ke %s (di luar daftar alias yang dikenali: %s)."
                % (alias, ", ".join(KNOWN_MODELS)),
            )
        return

    send_message(chat_id, "Perintah tidak dikenali. Ketik /help untuk daftar perintah.")


# --------------------------------------------------------------------------
# Long polling
# --------------------------------------------------------------------------
def validate_config() -> None:
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN kosong. Isi di .env lalu jalankan ulang.")
        sys.exit(1)
    if not ALLOWED_CHATS:
        log.error("ALLOWED_CHATS kosong. Isi minimal satu chat id di .env lalu jalankan ulang.")
        sys.exit(1)
    if not MASTER_KEY:
        log.warning("MASTER_KEY kosong — permintaan ke gateway kemungkinan ditolak.")


def main() -> None:
    validate_config()
    log.info(
        "Bot mulai. Gateway=%s | model default=%s | memori=%d pesan | whitelist chat: %s",
        LITELLM_URL,
        DEFAULT_MODEL,
        MEMORY_LEN,
        ", ".join(sorted(ALLOWED_CHATS)),
    )

    offset = None
    backoff = 1
    while True:
        payload = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        try:
            data = tg_call("getUpdates", payload, timeout=POLL_TIMEOUT + 15)
            backoff = 1
        except Exception as exc:  # noqa: BLE001
            log.warning("getUpdates gagal: %s. Coba lagi dalam %d detik.", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        for update in data.get("result", []):
            update_id = update.get("update_id")
            if update_id is not None:
                offset = (offset or 0)
                offset = max(offset, update_id + 1)
            message = update.get("message")
            if not message:
                continue
            try:
                handle_message(message)
            except Exception:  # noqa: BLE001
                log.exception("Gagal memproses update %s", update_id)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Bot dihentikan.")
