#!/usr/bin/env python3
"""Uji mandiri offline untuk link-tools.

Menjalankan server HTTP lokal sebagai tiruan situs (halaman OG, pengalihan,
oEmbed) lalu memeriksa perilaku pemeriksa tautan. Tidak butuh internet.

Jalankan:  python3 selftest.py
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import linkinspect
from linkinspect import normalize, parse_meta, render_text
from bot import extract_urls, split_text

PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print("  ok   %s" % label)
    else:
        FAIL += 1
        print("  FAIL %s %s" % (label, detail))


# --------------------------------------------------------------------------
# Server tiruan
# --------------------------------------------------------------------------
PAGE = """<!doctype html><html lang="id"><head>
<meta charset="utf-8">
<title>Judul Fallback</title>
<meta property="og:title" content="Judul Kartu &amp; Uji">
<meta property="og:description" content="Deskripsi kartu pratinjau.">
<meta property="og:site_name" content="Situs Uji">
<meta property="og:image" content="https://cdn.example.test/gambar.jpg">
<meta property="og:type" content="article">
<link rel="canonical" href="/kanonik">
</head><body>Halo</body></html>"""

MEDIA = b"\x00\x01\x02fake-video-bytes"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):  # senyap
        pass

    def _send(self, code: int, ctype: str, body: bytes, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        self.path = path
        if self.path.startswith("/media"):
            self._send(200, "video/mp4", b"", {"Content-Length": str(len(MEDIA))})
        elif self.path.startswith("/short"):
            self._send(302, "text/plain", b"", {"Location": "/halaman"})
        elif self.path == "/rantai":
            self._send(301, "text/plain", b"", {"Location": "/short"})
        elif self.path == "/oembed.json":
            body = json.dumps(
                {
                    "title": "Video Uji",
                    "author_name": "Kreator Uji",
                    "provider_name": "Situs Uji",
                    "thumbnail_url": "https://cdn.example.test/thumb.jpg",
                    "type": "video",
                    "html": '<iframe src="https://player.example.test/v/123"></iframe>',
                }
            ).encode()
            self._send(200, "application/json", body)
        elif self.path == "/robots.txt":
            self._send(200, "text/plain", b"User-agent: *\nDisallow: /privat\n")
        elif self.path.startswith("/privat"):
            self._send(200, "text/html", PAGE.encode())
        else:
            self._send(200, "text/html", PAGE.encode())

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        self.path = path
        if self.path.startswith("/media"):
            self._send(200, "video/mp4", MEDIA)
        elif self.path.startswith("/short"):
            self._send(302, "text/plain", b"", {"Location": "/halaman"})
        elif self.path == "/rantai":
            self._send(301, "text/plain", b"", {"Location": "/short"})
        elif self.path == "/oembed.json":
            self.do_HEAD()
        elif self.path == "/robots.txt":
            self.do_HEAD()
        elif self.path.startswith("/lamat"):
            self._send(429, "text/plain", b"slow down", {"Retry-After": "30"})
        else:
            self._send(200, "text/html", PAGE.encode())


def serve() -> tuple[HTTPServer, str]:
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d" % httpd.server_address[1]


# --------------------------------------------------------------------------
# Uji
# --------------------------------------------------------------------------
def main() -> int:
    print("1. Pengamanan URL (anti-SSRF) tanpa jaringan")
    for bad, why in [
        ("http://localhost/x", "localhost"),
        ("http://127.0.0.1/x", "loopback"),
        ("http://169.254.169.254/latest/meta-data/", "metadata cloud"),
        ("http://10.0.0.5/", "IP privat"),
        ("http://[::1]/", "IPv6 loopback"),
        ("file:///etc/passwd", "skema file"),
        ("ftp://contoh.id/x", "skema ftp"),
        ("http://user:pass@contoh.id/", "kredensial"),
        ("http://printer.internal/", "suffix internal"),
    ]:
        try:
            linkinspect.guard_url(bad)
            check("tolak %s (%s)" % (bad, why), False, "-> justru diterima")
        except linkinspect.UnsafeURL:
            check("tolak %s (%s)" % (bad, why), True)

    check("protokol dokumentasi publik lolos", linkinspect.guard_url("http://93.184.215.14/") == "http://93.184.215.14/")

    print("2. Normalisasi & ekstraksi")
    check("tanpa skema -> https", normalize("contoh.id/artikel") == "https://contoh.id/artikel")
    check("sudut <> dibuang", normalize("<https://contoh.id>") == "https://contoh.id")
    urls = extract_urls("liat ini https://a.id/x dan t.co/abc, plus www.b.id/y")
    check("deteksi 3 tautan", len(urls) == 3, str(urls))
    check("koma tidak ikut", urls[1] == "t.co/abc", urls[1] if len(urls) > 1 else "")

    print("3. Parser Open Graph")
    meta = parse_meta(PAGE)
    check("og:title menang atas <title>", meta["title"] == "Judul Kartu & Uji", meta["title"])
    check("entitas &amp; di-decode", "&" in meta["title"])
    check("deskripsi", meta["description"].startswith("Deskripsi"))
    check("gambar", meta["image"].endswith("gambar.jpg"))
    check("kanonik", meta["canonical"] == "/kanonik")
    check("locale dari lang", meta["locale"] == "id", meta["locale"])
    check("HTML rusak tidak meledak", parse_meta("<html><meta property=")["title"] == "")

    print("4. Pemecah pesan Telegram")
    chunks = split_text("x" * 9000, limit=4000)
    check("panjang chunk <= limit", all(len(c) <= 4000 for c in chunks), str([len(c) for c in chunks]))
    check("semua isi tersambung", sum(len(c) for c in chunks) >= 9000)

    print("5. Server lokal: pengalihan, metadata, oEmbed, media")
    httpd, base = serve()
    original_guard = linkinspect.guard_url
    linkinspect.guard_url = lambda url: url  # hanya di dalam uji: izinkan loopback
    try:
        report = linkinspect.inspect(base + "/rantai", use_oembed=False)
        check("rantai 2 hop tercatat", len(report["chain"]) == 2, str(report["chain"]))
        check("URL akhir /halaman", report["final_url"].endswith("/halaman"), report["final_url"])
        check("judul OG terbaca", report["meta"]["title"] == "Judul Kartu & Uji")
        check("status 200", report["status"] == 200)

        media = linkinspect.inspect(base + "/media.mp4")
        check("media tidak diunduh", media["truncated"] is True)
        check("tipe media terdeteksi", "video/mp4" in media["content_type"], media["content_type"])
        check("panjang berkas dari header", media["content_length"] == len(MEDIA))
        check("catatan media ada", any("dibaca" in n for n in media["notes"]), str(media["notes"]))

        limited = linkinspect.inspect(base + "/lamat")
        check("HTTP 429 dilaporkan", limited["status"] == 429, str(limited["status"]))
        check("saran rate limit", any("rate limit" in (limited["error"] or "") for _ in [0]))

        blocked = linkinspect.inspect(base + "/privat")
        check("robots.txt dihormati", blocked["robots_blocked"] is True)
        check("halaman terlarang tidak diambil", blocked["meta"] == {})
        allowed = linkinspect.inspect(base + "/privat", use_robots=False)
        check("--no-robots menembus larangan", allowed["meta"].get("site_name") == "Situs Uji")

        fake = {"name": "Situs Uji", "hosts": "", "oembed": base + "/oembed.json?url={url}", "kind": "video"}
        data, note = linkinspect.fetch_oembed(fake, base + "/halaman")
        check("oEmbed terbaca", note is None and data is not None, str(note))
        check("iframe embed diekstrak", data["embed"] == "https://player.example.test/v/123", json.dumps(data))
        check("thumbnail oEmbed", data["thumbnail"].endswith("thumb.jpg"))

        text = render_text(report, html=True)
        check("teks tanpa tag liar", "<script" not in text)
        check("teks memuat judul", "Judul Kartu" in text)
        check("teks memuat pengalihan", "Pengalihan" in text)
    finally:
        linkinspect.guard_url = original_guard
        httpd.shutdown()

    print("\n%d lolos, %d gagal" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
