#!/usr/bin/env python3
"""Pemeriksa tautan (link inspector) — hanya standard library.

Alur kerja:
  1. Validasi + lindungi dari SSRF (tolak localhost / IP privat).
  2. Lacak pengalihan (bit.ly, t.co, dst.) sampai URL akhir, tampilkan rantainya.
  3. Kenali penyedia resmi (YouTube, Vimeo, Spotify, ...) dan ambil metadata
     lewat endpoint oEmbed RESMI milik penyedia itu sendiri.
  4. Lengkapi dengan metadata Open Graph / Twitter Card untuk kartu pratinjau.
  5. Sajikan sebagai teks ringkas (untuk Telegram) atau JSON.

Yang SENGAJA tidak dilakukan oleh alat ini:
  - Tidak mengekstrak stream video/audio dari situs penonton streaming.
  - Tidak memproses playlist HLS (.m3u8), DASH (.mpd), atau segmen .ts.
  - Tidak memalsukan Referer/User-Agent untuk menembus proteksi hotlink.
  - Tidak mengunduh berkas media (isi non-HTML hanya dibaca headernya saja).

Singkatnya: ini setara kartu pratinjau tautan di aplikasi pesan, bukan pengunduh.
"""

from __future__ import annotations

import argparse
import html as html_mod
import ipaddress
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser

VERSION = "1.0"

# --------------------------------------------------------------------------
# Konfigurasi (semua bisa dioverride lewat environment)
# --------------------------------------------------------------------------
USER_AGENT = os.environ.get(
    "LINKBOT_UA",
    "nolims-linkbot/1.0 (+pemeriksa tautan; kartu pratinjau)",
)
TIMEOUT = float(os.environ.get("LINKBOT_TIMEOUT", "12"))
MAX_BYTES = int(os.environ.get("LINKBOT_MAX_BYTES", str(512 * 1024)))
MAX_REDIRECTS = int(os.environ.get("LINKBOT_MAX_REDIRECTS", "8"))
RESPECT_ROBOTS = os.environ.get("LINKBOT_RESPECT_ROBOTS", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)

REDIRECT_CODES = (301, 302, 303, 307, 308)
TEXTUAL_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "application/json",
    "text/plain",
)

BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".home.arpa", ".test", ".invalid")


# --------------------------------------------------------------------------
# Pengamanan URL (anti-SSRF)
# --------------------------------------------------------------------------
class UnsafeURL(ValueError):
    """URL menunjuk ke jaringan privat / skema yang tidak didukung."""


def guard_url(url: str) -> str:
    """Pastikan URL aman diambil: skema http/https, host publik, tanpa kredensial."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("skema '%s' tidak didukung (hanya http/https)" % parts.scheme)
    if parts.username or parts.password:
        raise UnsafeURL("URL memuat kredensial (user:pass) — ditolak")

    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise UnsafeURL("URL tanpa nama host")
    if host in ("localhost", "localhost.") or host.endswith(BLOCKED_SUFFIXES):
        raise UnsafeURL("host '%s' bersifat lokal" % host)

    # Host berupa IP literal.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if _is_private_ip(ip):
            raise UnsafeURL("IP '%s' berada di jaringan privat" % host)
        return url

    # Host berupa nama: resolusi DNS dan tolak bila ada alamat privat.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeURL("gagal resolusi DNS untuk '%s': %s" % (host, exc)) from None

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_private_ip(ip):
            raise UnsafeURL("host '%s' menunjuk ke jaringan privat (%s)" % (host, ip))
    return url


def _is_private_ip(ip: ipaddress._BaseAddress) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def normalize(raw: str) -> str:
    """Rapikan masukan pengguna menjadi URL utuh."""
    text = (raw or "").strip().strip("<>\"'")
    if not text:
        raise ValueError("tautan kosong")
    if not urllib.parse.urlsplit(text).scheme:
        text = "https://" + text.lstrip("/")
    return text


# --------------------------------------------------------------------------
# Pengambilan HTTP
# --------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Kita ikuti pengalihan sendiri agar rantainya bisa dilaporkan dan dijaga."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _raw_open(url: str, method: str = "GET"):
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header(
        "Accept",
        "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.5",
    )
    req.add_header("Accept-Language", "id,en;q=0.8")
    return _OPENER.open(req, timeout=TIMEOUT)


def fetch(url: str, *, method: str = "GET", follow: bool = True) -> dict:
    """Ambil URL dan kembalikan dict berisi status, header, isi (bila tekstual)."""
    chain: list[dict] = []
    current = url

    for _hop in range(MAX_REDIRECTS + 1):
        try:
            guard_url(current)
        except UnsafeURL as exc:
            return _failure(current, "URL ditolak: %s" % exc, chain)

        try:
            resp = _raw_open(current, method=method)
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else None
            if follow and exc.code in REDIRECT_CODES and location:
                target = urllib.parse.urljoin(current, location)
                chain.append({"from": current, "to": target, "status": exc.code})
                current = target
                continue
            detail = ""
            if exc.headers and exc.headers.get("Retry-After"):
                detail = " (Retry-After: %s)" % exc.headers["Retry-After"]
            note = "%s %s%s" % (exc.code, exc.reason or "HTTP error", detail)
            if exc.code in (401, 403):
                note += " — situs menolak bot pratinjau"
            if exc.code == 429:
                note += " — kena rate limit"
            return _failure(current, note, chain, status=exc.code)
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            return _failure(current, "gagal koneksi: %s" % getattr(exc, "reason", exc), chain)
        except Exception as exc:  # noqa: BLE001 - apa pun tidak boleh mematikan bot
            return _failure(current, "error tak terduga: %s" % exc, chain)

        with resp:
            status = getattr(resp, "status", 200)
            headers = {k.lower(): v for k, v in resp.headers.items()}
            ctype = (headers.get("content-type") or "").split(";")[0].strip().lower()
            textual = ctype.startswith(TEXTUAL_TYPES) or not ctype
            declared = headers.get("content-length")

            if textual:
                body = resp.read(MAX_BYTES + 1)
                truncated = len(body) > MAX_BYTES
                body = body[:MAX_BYTES]
            else:
                # Berkas media/biner: cukup headernya, isinya TIDAK diunduh.
                body = b""
                truncated = True

            return {
                "url": current,
                "final_url": current,
                "status": status,
                "reason": getattr(resp, "reason", "") or "",
                "headers": headers,
                "content_type": ctype or "tidak diketahui",
                "content_length": int(declared) if (declared or "").isdigit() else None,
                "charset": _charset(headers.get("content-type", "")),
                "body": body,
                "truncated": truncated,
                "chain": chain,
                "error": None,
            }

    return _failure(current, "terlalu banyak pengalihan (maks %d)" % MAX_REDIRECTS, chain)


def _failure(url: str, message: str, chain: list[dict], status: int | None = None) -> dict:
    return {
        "url": url,
        "final_url": url,
        "status": status,
        "reason": "",
        "headers": {},
        "content_type": "",
        "content_length": None,
        "charset": "utf-8",
        "body": b"",
        "truncated": False,
        "chain": chain,
        "error": message,
    }


def _charset(content_type: str) -> str:
    match = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    return match.group(1) if match else "utf-8"


def expand(raw: str) -> dict:
    """Lacak pengalihan tanpa mengambil isi halaman (metode HEAD)."""
    url = normalize(raw)
    result = fetch(url, method="HEAD", follow=True)
    if result["error"] and result["status"] in (405, 501):
        result = fetch(url, method="GET", follow=True)  # server tolak HEAD
    return result


# --------------------------------------------------------------------------
# Kepatuhan robots.txt (fail-open: kalau tidak terbaca, izinkan)
# --------------------------------------------------------------------------
_ROBOTS_CACHE: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def robots_allowed(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    origin = "%s://%s" % (parts.scheme, parts.netloc)
    if origin not in _ROBOTS_CACHE:
        parser = urllib.robotparser.RobotFileParser()
        try:
            with _raw_open(urllib.parse.urljoin(origin, "/robots.txt")) as resp:
                raw = resp.read(256 * 1024).decode("utf-8", "replace")
            parser.parse(raw.splitlines())
            _ROBOTS_CACHE[origin] = parser
        except Exception:  # noqa: BLE001 - tanpa robots.txt => izinkan
            _ROBOTS_CACHE[origin] = None
    parser = _ROBOTS_CACHE[origin]
    if parser is None:
        return True
    try:
        return parser.can_fetch(USER_AGENT, url)
    except Exception:  # noqa: BLE001
        return True


# --------------------------------------------------------------------------
# Registry penyedia resmi (oEmbed)
# --------------------------------------------------------------------------
PROVIDERS: list[dict] = [
    {
        "name": "YouTube",
        "hosts": r"(^|\.)(youtube\.com|youtu\.be|youtube-nocookie\.com)$",
        "oembed": "https://www.youtube.com/oembed?format=json&url={url}",
        "kind": "video",
    },
    {
        "name": "Vimeo",
        "hosts": r"(^|\.)vimeo\.com$",
        "oembed": "https://vimeo.com/api/oembed.json?url={url}",
        "kind": "video",
    },
    {
        "name": "Dailymotion",
        "hosts": r"(^|\.)dailymotion\.com$",
        "oembed": "https://www.dailymotion.com/services/oembed?format=json&url={url}",
        "kind": "video",
    },
    {
        "name": "SoundCloud",
        "hosts": r"(^|\.)soundcloud\.com$",
        "oembed": "https://soundcloud.com/oembed?format=json&url={url}",
        "kind": "audio",
    },
    {
        "name": "Spotify",
        "hosts": r"(^|\.)spotify\.com$",
        "oembed": "https://open.spotify.com/oembed?url={url}",
        "kind": "audio",
    },
    {
        "name": "TikTok",
        "hosts": r"(^|\.)tiktok\.com$",
        "oembed": "https://www.tiktok.com/oembed?url={url}",
        "kind": "video",
    },
    {
        "name": "X / Twitter",
        "hosts": r"(^|\.)(twitter\.com|x\.com)$",
        "oembed": "https://publish.twitter.com/oembed?url={url}",
        "kind": "post",
    },
    {
        "name": "Reddit",
        "hosts": r"(^|\.)reddit\.com$",
        "oembed": "https://www.reddit.com/oembed?url={url}",
        "kind": "post",
    },
    {
        "name": "Flickr",
        "hosts": r"(^|\.)flickr\.com$",
        "oembed": "https://www.flickr.com/services/oembed/?format=json&url={url}",
        "kind": "image",
    },
    {
        "name": "Tumblr",
        "hosts": r"(^|\.)tumblr\.com$",
        "oembed": "https://www.tumblr.com/oembed/1.0?url={url}",
        "kind": "post",
    },
    {
        "name": "Kickstarter",
        "hosts": r"(^|\.)kickstarter\.com$",
        "oembed": "https://www.kickstarter.com/services/oembed?url={url}",
        "kind": "post",
    },
]

# Layanan agregator oEmbed publik; dipakai HANYA bila penyedia tidak punya endpoint
# sendiri di daftar di atas. Matikan dengan LINKBOT_NOEMBED=0.
NOEMBED_HOSTS = r"(^|\.)(instagram\.com|facebook\.com|twitch\.tv|imgur\.com)$"
NOEMBED_ENABLED = os.environ.get("LINKBOT_NOEMBED", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)


def detect_provider(url: str) -> dict | None:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    for provider in PROVIDERS:
        if re.search(provider["hosts"], host):
            return provider
    if NOEMBED_ENABLED and re.search(NOEMBED_HOSTS, host):
        return {
            "name": host.split(".")[-2].capitalize() if "." in host else host,
            "hosts": NOEMBED_HOSTS,
            "oembed": "https://noembed.com/embed?url={url}",
            "kind": "post",
        }
    return None


IFRAME_SRC = re.compile(r"""<iframe[^>]*\ssrc=["']([^"']+)["']""", re.I)


def fetch_oembed(provider: dict, url: str) -> tuple[dict | None, str | None]:
    """Ambil metadata dari endpoint oEmbed resmi penyedia."""
    endpoint = provider["oembed"].format(url=urllib.parse.quote(url, safe=""))
    try:
        guard_url(endpoint)
    except UnsafeURL as exc:
        return None, "endpoint oEmbed ditolak: %s" % exc

    try:
        with _raw_open(endpoint) as resp:
            raw = resp.read(256 * 1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return None, "oEmbed gagal: HTTP %s" % exc.code
    except Exception as exc:  # noqa: BLE001
        return None, "oEmbed gagal: %s" % exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, "oEmbed mengembalikan bukan JSON"
    if isinstance(data, dict) and data.get("error"):
        return None, "oEmbed menolak: %s" % data["error"]
    if not isinstance(data, dict):
        return None, "format oEmbed tidak dikenali"

    html = data.get("html") or ""
    match = IFRAME_SRC.search(html)
    return (
        {
            "title": (data.get("title") or "").strip(),
            "author": (data.get("author_name") or "").strip(),
            "author_url": (data.get("author_url") or "").strip(),
            "provider": (data.get("provider_name") or provider["name"]).strip(),
            "thumbnail": (data.get("thumbnail_url") or "").strip(),
            "kind": data.get("type") or provider["kind"],
            # iframe di bawah ini berasal dari kode embed RESMI penyedia.
            "embed": match.group(1) if match else "",
        },
        None,
    )


# --------------------------------------------------------------------------
# Metadata Open Graph / Twitter Card
# --------------------------------------------------------------------------
class MetaParser(HTMLParser):
    """Ambil <title>, tag meta pratinjau, dan rel=canonical. Tanpa isi media."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.canonical = ""
        self.lang = ""
        self.meta: dict[str, str] = {}
        self._in_title = False

    def handle_starttag(self, tag, attrs):  # noqa: D102
        attr = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html" and attr.get("lang"):
            self.lang = attr["lang"].strip()
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (attr.get("property") or attr.get("name") or "").strip().lower()
            content = (attr.get("content") or "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag == "link" and "canonical" in (attr.get("rel") or "").lower():
            self.canonical = (attr.get("href") or "").strip()

    def handle_endtag(self, tag):  # noqa: D102
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):  # noqa: D102
        if self._in_title:
            self.title += data


def parse_meta(html: str) -> dict:
    parser = MetaParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - HTML rusak jangan sampai menggagalkan bot
        pass
    meta = parser.meta

    def pick(*keys: str) -> str:
        for key in keys:
            value = meta.get(key, "").strip()
            if value:
                return value
        return ""

    return {
        "title": (pick("og:title", "twitter:title") or " ".join(parser.title.split())).strip(),
        "description": pick("og:description", "twitter:description", "description")[:600],
        "site_name": pick("og:site_name", "application-name"),
        "image": pick("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src"),
        "media_hint": pick("og:video:secure_url", "og:video:url", "og:video", "twitter:player"),
        "type": pick("og:type"),
        "locale": pick("og:locale") or parser.lang,
        "canonical": parser.canonical,
    }


# --------------------------------------------------------------------------
# Laporan
# --------------------------------------------------------------------------
def inspect(raw: str, *, use_oembed: bool = True, use_robots: bool = True) -> dict:
    """Periksa satu tautan dan kembalikan laporan lengkap sebagai dict."""
    try:
        url = normalize(raw)
        guard_url(url)
    except (ValueError, UnsafeURL) as exc:
        return {"input": raw, "url": raw, "error": str(exc), "status": None}

    report: dict = {
        "input": raw,
        "url": url,
        "provider": None,
        "oembed": None,
        "meta": {},
        "robots_blocked": False,
        "notes": [],
        "error": None,
        "status": None,
        "final_url": url,
        "chain": [],
        "content_type": "",
        "content_length": None,
    }

    provider = detect_provider(url) if use_oembed else None
    if provider:
        report["provider"] = provider["name"]

    if use_robots and RESPECT_ROBOTS and not robots_allowed(url):
        report["robots_blocked"] = True
        report["notes"].append(
            "robots.txt situs melarang pengambilan otomatis halaman ini — halaman tidak diambil."
        )
        result = _failure(url, None, [])  # type: ignore[arg-type]
        result["error"] = None
    else:
        result = fetch(url, follow=True)

    report["status"] = result["status"]
    report["final_url"] = result["final_url"]
    report["chain"] = result["chain"]
    report["content_type"] = result["content_type"]
    report["content_length"] = result["content_length"]
    report["error"] = result["error"]
    report["truncated"] = result["truncated"]

    if provider and report["final_url"] != url:
        provider = detect_provider(report["final_url"]) or provider
        report["provider"] = provider["name"]

    if use_oembed and provider and not report["error"]:
        data, note = fetch_oembed(provider, report["final_url"])
        report["oembed"] = data
        if note:
            report["notes"].append(note)

    if result["body"]:
        try:
            text = result["body"].decode(result["charset"] or "utf-8", "replace")
        except LookupError:
            text = result["body"].decode("utf-8", "replace")
        report["meta"] = parse_meta(text)
    elif report["error"] is None and "html" not in (result["content_type"] or ""):
        report["notes"].append(
            "Isi bukan HTML (%s) — header saja yang dibaca, berkas tidak diunduh."
            % (result["content_type"] or "tipe tidak diketahui")
        )

    return report


def human_size(num: int | None) -> str:
    if not num:
        return "?"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return "%.0f %s" % (value, unit) if unit == "B" else "%.1f %s" % (value, unit)
        value /= 1024
    return "%d B" % num


def _hop_label(url: str) -> str:
    """Label ringkas satu hop pengalihan: host + jalur."""
    parts = urllib.parse.urlsplit(url)
    return "%s%s" % (parts.netloc, _shorten(parts.path, 30))


def _shorten(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_text(report: dict, *, html: bool = False) -> str:
    """Susun laporan menjadi teks ringkas (aman untuk Telegram)."""
    esc = (lambda s: html_mod.escape(str(s), quote=False)) if html else (lambda s: str(s))

    if report.get("error") and not report.get("final_url"):
        return "❌ %s" % esc(report["error"])
    if report.get("error") and report.get("status") is None and not report.get("chain"):
        return "❌ %s\n%s" % (esc(report["error"]), esc(report.get("url", "")))

    oembed = report.get("oembed") or {}
    meta = report.get("meta") or {}
    lines: list[str] = []

    title = oembed.get("title") or meta.get("title") or report.get("final_url", "")
    lines.append("🔗 %s" % esc(title[:200]))

    author = oembed.get("author")
    if author:
        lines.append("👤 %s" % esc(author[:120]))

    if report.get("provider"):
        lines.append("🏷️ Penyedia: %s%s" % (esc(report["provider"]), " (oEmbed resmi)" if oembed else ""))
    elif meta.get("site_name"):
        lines.append("🏷️ Situs: %s" % esc(meta["site_name"][:120]))

    description = meta.get("description")
    if description:
        lines.append("📝 %s" % esc(description[:280]))

    if oembed.get("thumbnail") or meta.get("image"):
        lines.append("🖼️ %s" % esc(oembed.get("thumbnail") or meta["image"]))

    embed = oembed.get("embed") or ""
    if embed:
        lines.append("▶️ Embed resmi: %s" % esc(_shorten(embed, 240)))
    elif meta.get("media_hint"):
        lines.append("▶️ Petunjuk media (OG): %s" % esc(_shorten(meta["media_hint"], 240)))

    if meta.get("canonical") and meta["canonical"] != report.get("final_url"):
        lines.append("📎 Kanonik: %s" % esc(meta["canonical"]))

    chain = report.get("chain") or []
    if chain:
        hops = " → ".join(
            [_hop_label(chain[0]["from"])] + [_hop_label(step["to"]) for step in chain]
        )
        lines.append("↪️ Pengalihan (%d): %s" % (len(chain), esc(_shorten(hops, 300))))
    lines.append("🌐 URL akhir: %s" % esc(report.get("final_url", "")))

    status = report.get("status")
    detail = ""
    if status is not None:
        detail = " · %s" % report.get("content_type", "")
        if report.get("content_length"):
            detail += " · %s" % human_size(report["content_length"])
        status_line = "📡 HTTP %s%s" % (status, esc(detail))
    else:
        status_line = "📡 Tidak ada respons HTTP"
    lines.append(status_line)

    if report.get("truncated") and "html" in (report.get("content_type") or ""):
        lines.append("✂️ Halaman dipotong pada batas baca metadata.")

    for note in report.get("notes", []):
        lines.append("ℹ️ %s" % esc(note))
    if report.get("error"):
        lines.append("⚠️ %s" % esc(report["error"]))

    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="inspect.py",
        description="Pemeriksa tautan: perluas shortlink, kenali penyedia, ambil metadata pratinjau.",
    )
    parser.add_argument("urls", nargs="+", help="satu atau beberapa tautan")
    parser.add_argument("--json", action="store_true", help="keluarkan JSON, bukan teks")
    parser.add_argument("--no-oembed", action="store_true", help="lewati endpoint oEmbed")
    parser.add_argument("--no-robots", action="store_true", help="abaikan robots.txt")
    parser.add_argument("--version", action="version", version="link-inspector %s" % VERSION)
    args = parser.parse_args(argv)

    exit_code = 0
    for raw in args.urls:
        report = inspect(raw, use_oembed=not args.no_oembed, use_robots=not args.no_robots)
        if args.json:
            printable = {k: v for k, v in report.items() if k != "body"}
            print(json.dumps(printable, indent=2, ensure_ascii=False))
        else:
            print(render_text(report))
            print()
        if report.get("error"):
            exit_code = 1
    return exit_code


# Alias agar pemanggil tidak tertukar dengan modul standar `inspect`.
inspect_link = inspect


if __name__ == "__main__":
    sys.exit(main())
