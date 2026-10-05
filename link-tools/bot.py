#!/usr/bin/env python3
"""Bot Telegram pemeriksa tautan (long-polling, stdlib only).

Kirim tautan apa pun ke bot ini, dan bot akan membalas kartu pratinjau:
judul, penulis, deskripsi, gambar, embed RESMI (bila penyedia punya),
URL akhir setelah pengalihan, dan status HTTP-nya.

Perintah:
  /start, /help         — bantuan
  /expand <tautan>      — hanya lacak pengalihan shortlink
  /json <tautan>        — keluarkan laporan mentah (JSON)
  tautan biasa          — kartu pratinjau (maks 3 tautan per pesan)

Batasan yang disengaja lihat docstring link-tools/linkinspect.py: bot ini tidak
mengekstrak stream video, tidak memproses HLS/m3u8, dan tidak mengunduh media.
"""

from __future__ import annotations

import html as html_mod
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

from linkinspect import expand, human_size, inspect_link, render_text

# --------------------------------------------------------------------------
# Konfigurasi
# --------------------------------------------------------------------------
BOT_TOKEN = (
    os.environ.get("LINKBOT_TOKEN", "").strip()
    or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
)
ALLOWED_CHATS = {
    c.strip() for c in os.environ.get("ALLOWED_CHATS", "").split(",") if c.strip()
}
POLL_TIMEOUT = int(os.environ.get("LINKBOT_POLL_TIMEOUT", "50"))
MAX_CHARS = int(os.environ.get("LINKBOT_MAX_CHARS", "4096"))
MAX_LINKS_PER_MESSAGE = int(os.environ.get("LINKBOT_MAX_LINKS", "3"))
SEND_RETRIES = int(os.environ.get("LINKBOT_SEND_RETRIES", "3"))
SEND_PHOTO = os.environ.get("LINKBOT_SEND_PHOTO", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)

TELEGRAM_API = "https://api.telegram.org/bot%s" % BOT_TOKEN

logging.basicConfig(
    level=os.environ.get("LINKBOT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("linkbot")

URL_RE = re.compile(
    r"https?://[^\s<>\"']+"          # URL dengan skema
    r"|(?<![\w./-])www\.[^\s<>\"']+"  # www.tanpa-skema
    r"|(?<![\w./@-])[a-z0-9-]+(?:\.[a-z]{2,})+(?:/[^\s<>\"']*)?"  # contoh.id/jalur
)

HELP_TEXT = (
    "🔗 <b>Pemeriksa Tautan</b>\n"
    "\n"
    "Kirim tautan apa pun (atau ketik langsung), bot akan membuat kartu pratinjau:\n"
    "judul, penulis, deskripsi, gambar, embed resmi, URL akhir, dan status HTTP.\n"
    "\n"
    "Perintah:\n"
    "  /expand &lt;tautan&gt;  — lacak pengalihan shortlink saja\n"
    "  /json &lt;tautan&gt;    — laporan mentah (JSON)\n"
    "  /help            — pesan ini\n"
    "\n"
    "<b>Yang tidak dilakukan bot ini:</b> tidak mengekstrak stream dari situs\n"
    "penonton streaming, tidak memproses HLS (.m3u8), dan tidak mengunduh berkas media.\n"
    "Metadata diambil dari oEmbed resmi penyedia dan tag Open Graph — setara kartu\n"
    "pratinjau di aplikasi pesan.\n"
    "\n"
    "Maks %d tautan per pesan." % MAX_LINKS_PER_MESSAGE
)


# --------------------------------------------------------------------------
# Utilitas Telegram
# --------------------------------------------------------------------------
def tg_call(method: str, payload: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        "%s/%s" % (TELEGRAM_API, method), data=data, method="POST" if data else "GET"
    )
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError("HTTP %s dari Telegram: %s" % (exc.code, detail)) from None
    return json.loads(body) if body else {}


def send_message(chat_id: str, text: str, *, preview: bool = False) -> bool:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": not preview,
    }
    for attempt in range(1, SEND_RETRIES + 1):
        try:
            tg_call("sendMessage", payload)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Gagal kirim pesan (%d/%d): %s", attempt, SEND_RETRIES, exc)
            if attempt < SEND_RETRIES:
                time.sleep(2 * attempt)
    return False


def send_photo(chat_id: str, photo_url: str, caption: str) -> bool:
    """Kirim kartu bergambar lewat sendPhoto (Telegram mengunduh gambarnya sendiri)."""
    if not SEND_PHOTO or not photo_url or len(caption) > 1024:
        return False
    try:
        tg_call(
            "sendPhoto",
            {"chat_id": chat_id, "photo": photo_url, "caption": caption, "parse_mode": "HTML"},
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.info("sendPhoto gagal (%s), jatuh ke pesan teks.", exc)
        return False


def split_text(text: str, limit: int = MAX_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buffer = ""
    for line in text.splitlines(keepends=True):
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


def send_long(chat_id: str, text: str) -> None:
    for chunk in split_text(text):
        send_message(chat_id, chunk)


def send_pre(chat_id: str, raw: str) -> None:
    """Kirim blok <pre> (untuk JSON) dengan pemotongan aman."""
    escaped = html_mod.escape(raw, quote=False)
    budget = MAX_CHARS - len("<pre></pre>")
    for chunk in split_text(escaped, limit=budget):
        send_message(chat_id, "<pre>%s</pre>" % chunk)


def start_typing(chat_id: str) -> threading.Event:
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(4):
            try:
                tg_call("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=15)
            except Exception:  # noqa: BLE001
                pass

    try:
        tg_call("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=15)
    except Exception:  # noqa: BLE001
        pass
    threading.Thread(target=loop, name="typing-%s" % chat_id, daemon=True).start()
    return stop


# --------------------------------------------------------------------------
# Penanganan pesan
# --------------------------------------------------------------------------
def extract_urls(text: str) -> list[str]:
    found: list[str] = []
    for match in URL_RE.finditer(text or ""):
        candidate = match.group(0).rstrip(").,;:!?]}")
        if candidate.lower() in ("t.me", "telegram.me"):
            continue
        if candidate not in found:
            found.append(candidate)
    return found


def build_card(url: str) -> tuple[str, str]:
    """Kembalikan (teks_html, url_gambar) untuk satu tautan."""
    report = inspect_link(url)
    text = render_text(report, html=True)
    image = ""
    oembed = report.get("oembed") or {}
    meta = report.get("meta") or {}
    image = oembed.get("thumbnail") or meta.get("image") or ""
    return text, image


def drop_image_line(text: str) -> str:
    """Buang baris gambar dari keterangan foto (gambarnya sudah dikirim)."""
    return "\n".join(line for line in text.splitlines() if not line.startswith("🖼️"))


def handle_urls(chat_id: str, urls: list[str]) -> None:
    if len(urls) > MAX_LINKS_PER_MESSAGE:
        send_message(
            chat_id,
            "ℹ️ Ditemukan %d tautan; hanya %d pertama yang diperiksa."
            % (len(urls), MAX_LINKS_PER_MESSAGE),
        )
        urls = urls[:MAX_LINKS_PER_MESSAGE]

    stop = start_typing(chat_id)
    try:
        for index, url in enumerate(urls, start=1):
            prefix = "" if len(urls) == 1 else "%d/%d\n" % (index, len(urls))
            try:
                text, image = build_card(url)
            except Exception as exc:  # noqa: BLE001
                log.exception("Gagal memeriksa %s", url)
                text, image = "❌ Gagal memeriksa tautan: %s" % html_mod.escape(str(exc)), ""
            if prefix:
                text = prefix + text
            if image and send_photo(chat_id, image, drop_image_line(text)):
                continue
            send_long(chat_id, text)
    finally:
        stop.set()


def handle_command(chat_id: str, text: str) -> None:
    parts = text.split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""

    if command in ("/start", "/help"):
        send_message(chat_id, HELP_TEXT)
        return

    if command in ("/expand", "/json"):
        urls = extract_urls(argument) or ([argument] if argument else [])
        if not urls:
            send_message(chat_id, "Kirim tautannya juga, contoh: %s example.com/artikel" % command)
            return

        stop = start_typing(chat_id)
        try:
            for url in urls[:MAX_LINKS_PER_MESSAGE]:
                if command == "/json":
                    report = inspect_link(url)
                    payload = json.dumps(
                        {k: v for k, v in report.items() if k != "body"},
                        indent=2,
                        ensure_ascii=False,
                    )
                    send_pre(chat_id, payload)
                    continue

                result = expand(url)
                if result.get("error"):
                    send_message(chat_id, "❌ %s" % html_mod.escape(str(result["error"])))
                    continue
                lines = ["↪️ Pelacakan pengalihan"]
                for step in result.get("chain", []):
                    lines.append("  %s → %s" % (step["status"], html_mod.escape(step["to"])))
                lines.append("✅ URL akhir: %s" % html_mod.escape(result["final_url"]))
                if result.get("content_type"):
                    lines.append(
                        "📡 %s · %s"
                        % (
                            html_mod.escape(result["content_type"]),
                            human_size(result.get("content_length")),
                        )
                    )
                send_long(chat_id, "\n".join(lines))
        finally:
            stop.set()
        return

    send_message(chat_id, "Perintah tidak dikenali. Ketik /help untuk daftar perintah.")


def handle_message(message: dict) -> None:
    chat_id = str(message.get("chat", {}).get("id", ""))
    if chat_id not in ALLOWED_CHATS:
        log.info("Pesan dari chat %s diabaikan (tidak ada di ALLOWED_CHATS).", chat_id)
        return

    text = (message.get("text") or "").strip()
    if not text:
        send_message(chat_id, "Kirim tautan sebagai teks (mis. https://contoh.id/artikel).")
        return

    if text.startswith("/"):
        handle_command(chat_id, text)
        return

    urls = extract_urls(text)
    if not urls:
        send_message(
            chat_id,
            "Tidak ada tautan yang terdeteksi. Contoh: https://youtu.be/… atau t.co/…\n"
            "Ketik /help untuk bantuan.",
        )
        return

    handle_urls(chat_id, urls)


# --------------------------------------------------------------------------
# Long polling
# --------------------------------------------------------------------------
def validate_config() -> None:
    if not BOT_TOKEN:
        log.error("LINKBOT_TOKEN / TELEGRAM_BOT_TOKEN kosong. Isi di .env lalu jalankan ulang.")
        sys.exit(1)
    if not ALLOWED_CHATS:
        log.error("ALLOWED_CHATS kosong. Isi minimal satu chat id di .env lalu jalankan ulang.")
        sys.exit(1)


def main() -> None:
    validate_config()
    log.info(
        "Bot pemeriksa tautan mulai. Maks %d tautan/pesan | whitelist chat: %s",
        MAX_LINKS_PER_MESSAGE,
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
                offset = max(offset or 0, update_id + 1)
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
