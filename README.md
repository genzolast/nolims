# Personal AI Gateway + Bot Telegram

Gateway LLM pribadi berbasis **LiteLLM proxy** (API OpenAI-compatible) dengan cache Redis,
ditambah bot Telegram ber-long-polling. Semua berjalan di VPS Anda, proxy **hanya mendengarkan
di `127.0.0.1:4000`** — akses dari luar wajib lewat SSH tunnel.

---

## 1. Arsitektur

```
   Komputer Anda / Hermes                       VPS (Docker compose, jaringan pgw-net)
   ┌───────────────────────────┐                ┌──────────────────────────────────────────┐
   │ browser  http://127.0.0.1:4000/ui          │                                          │
   │ curl     http://127.0.0.1:4000/v1/...      │   ┌──────────────────────────────────┐   │
   │ Hermes   base_url http://127.0.0.1:4000/v1 │   │ proxy  (LiteLLM)                 │   │
   └───────────────┬───────────────────────────┘   │ 127.0.0.1:4000 -> 4000           │   │
                   │                               │ config: litellm-config.yaml (ro) │   │
           SSH tunnel:                             │ auth  : satu MASTER_KEY           │   │
     ssh -N -L 4000:127.0.0.1:4000 user@IP-VPS     │ cache : Redis (exact-match)       │   │
                   │                               └───────────┬───────────┬──────────┘   │
                   ▼                                           │           │              │
   ┌───────────────────────────┐                    ┌──────────▼──┐   ┌────▼───────────┐  │
   │ port 4000 hanya di VPS,   │                    │ redis       │   │ bot (opsional) │  │
   │ tidak terbuka ke publik   │                    │ 128mb LRU   │   │ long-polling   │  │
   └───────────────────────────┘                    │ volume data │   │ tanpa port     │  │
                                                    └─────────────┘   └───────┬────────┘  │
                                                                             │            │
                                                    Telegram API ◄───────────┘ keluar     │
                                                    (getUpdates / sendMessage)             │
                                                                                          │
   ┌──────────────────────────────────────────────────────────────────────────────────┐   │
   │ Upstream tier gratis:                                                            │   │
   │   Gemini 2.5 Flash / 2.5 Pro  (generativelanguage.googleapis.com/v1beta/openai/) │   │
   │   OpenRouter  xiaomi/mimo-v2-flash:free                                          │   │
   │   AIHubMix    xiaomi-mimo-v2.5-free                                              │   │
   └──────────────────────────────────────────────────────────────────────────────────┘   │
                                                                                          │
                                                    └──────────────────────────────────────┘
```

Alur: klien → proxy → (cache Redis hit?) → upstream. Bila upstream error/429/timeout,
proxy cooldown ±60 detik lalu mencoba alias berikutnya sesuai rantai fallback.

## 2. Daftar alias model

| Alias | Upstream | Model upstream | Kunci | rpm (dibatasi di config) |
|---|---|---|---|---|
| `pribadi-pro` | Google Gemini (OpenAI-compatible) | `gemini-2.5-flash` | `GEMINI_KEY` | ~10 |
| `pribadi-jenius` | Google Gemini (OpenAI-compatible) | `gemini-2.5-pro` | `GEMINI_KEY` | ~4 |
| `pribadi-hemat` | OpenRouter | `xiaomi/mimo-v2-flash:free` | `OPENROUTER_KEY` | ~15 |
| `pribadi-hemat-2` | AIHubMix | `xiaomi-mimo-v2.5-free` | `AIHUBMIX_KEY` | ~5 |

Rantai fallback (`router_settings.fallbacks` di `litellm-config.yaml`):

- `pribadi-jenius` → `pribadi-pro` → `pribadi-hemat` → `pribadi-hemat-2`
- `pribadi-pro` → `pribadi-hemat` → `pribadi-hemat-2`

## 3. Tempat mengambil kunci gratis

| Kunci | Tempat mengambil | Catatan |
|---|---|---|
| `GEMINI_KEY` | https://aistudio.google.com/apikey | Satu kunci dipakai Flash & Pro. Kuota Pro jauh lebih kecil. |
| `OPENROUTER_KEY` | https://openrouter.ai/keys | Pilih model `:free`, periksa kuota harian di dashboard. |
| `AIHubMix_KEY` | https://aihubmix.com (menu API key/token) | Cadangan terakhir, rpm dibatasi rendah. |
| `TELEGRAM_BOT_TOKEN` | Telegram → @BotFather → `/newbot` | Opsional, hanya bila memakai add-on bot AI. |
| `LINKBOT_TOKEN` | Telegram → @BotFather → `/newbot` (bot kedua) | Opsional, untuk add-on bot pemeriksa tautan. |
| `ALLOWED_CHATS` | @userinfobot / @getidsbot | Chat id Anda; beberapa id pisahkan dengan koma. |
| `MASTER_KEY` | Dibuat otomatis oleh `install.sh` | Random hex 32 byte; satu kunci untuk semua pemakaian. |

> Semua nilai disimpan di file `.env` yang **tidak pernah** di-commit (lihat `.gitignore`).
> Repo hanya berisi `.env.example` dengan nilai kosong.

## 4. Instalasi

```bash
git clone <URL-REPO> ~/personal-gateway
cd ~/personal-gateway
bash install.sh
```

`install.sh` melakukan, berurutan:

1. Memasang Docker Engine + plugin `docker compose` bila belum ada (butuh sudo/root hanya untuk bagian ini).
2. Bila `.env` belum ada: menyalin `.env.example` → `.env`, mengisi `MASTER_KEY` random hex 32 byte,
   lalu **berhenti** dan meminta Anda mengisi kunci gratis. Jalankan ulang `bash install.sh` setelah selesai.
3. Menjalankan `docker compose up -d` (proxy + redis). Overlay bot Telegram otomatis ikut hanya bila
   `TELEGRAM_BOT_TOKEN` dan `ALLOWED_CHATS` sudah terisi.
4. Health check ke `http://127.0.0.1:4000/health/liveliness`.
5. Menampilkan `MASTER_KEY` **satu kali** + cara akses.

## 5. Add-on bot Telegram

Isi `TELEGRAM_BOT_TOKEN` dan `ALLOWED_CHATS` di `.env`, lalu:

```bash
bash install-tele.sh
```

Skrip memvalidasi kedua nilai tersebut, menjalankan overlay
(`docker compose -f docker-compose.yml -f docker-compose.tele.yml up -d bot`),
dan menampilkan 5 baris terakhir log bot.

Perintah bot:

| Perintah | Fungsi |
|---|---|
| `/help` atau `/start` | Tampilkan bantuan + daftar alias |
| `/model` | Lihat model aktif di chat ini |
| `/model <alias>` | Ganti model untuk chat ini |
| `/reset` | Hapus memori percakapan chat ini |

Karakteristik bot:

- Python standard library saja (tanpa `pip install`), long-polling `getUpdates` timeout 50 detik.
- Tanpa webhook dan **tanpa port yang dipublikasikan** — bot hanya koneksi keluar ke Telegram.
- Memori 20 pesan terakhir per chat.
- Chat di luar `ALLOWED_CHATS` diabaikan **tanpa balasan apa pun**.
- Balasan >4096 karakter dipecah di batas baris; indikator `typing` dikirim selama menunggu;
  pengiriman pesan di-retry 3 kali.

## 6. Add-on bot pemeriksa tautan (link inspector)

Bot Telegram kedua (terpisah dari bot AI) yang mengubah tautan apa pun menjadi **kartu
pratinjau**: judul, penulis, deskripsi, gambar, embed resmi penyedia, URL akhir setelah
pengalihan, dan status HTTP-nya — cara kerjanya sama seperti pratinjau tautan di aplikasi pesan.

```bash
# isi LINKBOT_TOKEN (dari @BotFather) dan ALLOWED_CHATS di .env

bash install-link.sh
```

Perintah bot:

| Perintah | Fungsi |
|---|---|
| kirim tautan saja | kartu pratinjau (maks 3 tautan per pesan) |
| `/expand <tautan>` | lacak pengalihan shortlink (bit.ly, t.co, …) tanpa pratinjau |
| `/json <tautan>` | laporan mentah dalam JSON |
| `/help` | bantuan |

Sumber metadata, berurutan:

1. **Pengalihan** dilacak sendiri (maks 8 hop) dan rantainya ditampilkan, lalu URL akhir diperiksa ulang penyedianya.
2. **oEmbed resmi** untuk penyedia yang menyediakannya: YouTube, Vimeo, Dailymotion, SoundCloud, Spotify,
   TikTok, X/Twitter, Reddit, Flickr, Tumblr, Kickstarter — plus `noembed.com` untuk Instagram, Facebook,
   Twitch, dan Imgur.
3. Sisanya memakai tag **Open Graph / Twitter Card** (`og:title`, `og:description`, `og:image`, `og:video`).
4. Kode `<iframe>` yang ditampilkan selalu berasal dari respons oEmbed resmi penyedia.

Batas yang disengaja (lihat docstring `link-tools/linkinspect.py`):

- **Tidak** mengekstrak stream video/audio dari situs penonton streaming.
- **Tidak** memproses playlist HLS (`.m3u8`), DASH (`.mpd`), atau segmen `.ts`.
- **Tidak** memalsukan `Referer`/`User-Agent` untuk menembus proteksi hotlink.
- Berkas media (`mp4`, `m3u8`, `zip`, …) hanya dibaca **header**-nya; isinya tidak diunduh.
- Menghormati `robots.txt`: halaman yang dilarang tidak diambil (`LINKBOT_RESPECT_ROBOTS=0` untuk melewati).
- VPS terlindung dari SSRF: `localhost`, IP privat, `169.254.169.254`, dan skema non-`http(s)` ditolak.

Uji mandiri offline (tanpa internet, memakai server HTTP tiruan):

```bash
cd link-tools && python3 selftest.py
```

Konfigurasi opsional di `.env`:

| Variabel | Default | Fungsi |
|---|---|---|
| `LINKBOT_TOKEN` | nilai `TELEGRAM_BOT_TOKEN` | token bot; **pakai bot terpisah** — satu token hanya boleh punya satu `getUpdates` aktif |
| `LINKBOT_MAX_LINKS` | `3` | maksimum tautan yang diperiksa per pesan |
| `LINKBOT_TIMEOUT` | `12` | timeout HTTP per tautan (detik) |
| `LINKBOT_MAX_BYTES` | `524288` | batas baca HTML untuk metadata |
| `LINKBOT_MAX_REDIRECTS` | `8` | maksimum hop pengalihan |
| `LINKBOT_RESPECT_ROBOTS` | `1` | `0` untuk mengabaikan `robots.txt` |
| `LINKBOT_SEND_PHOTO` | `1` | `0` bila kartu bergambar tidak diinginkan |
| `LINKBOT_NOEMBED` | `1` | `0` untuk melarang pemakaian `noembed.com` |

## 7. Cara pakai

**a. SSH tunnel (wajib, karena proxy hanya di loopback VPS)**

```bash
ssh -N -L 4000:127.0.0.1:4000 user@IP-VPS
```

Biarkan perintah itu berjalan; lalu dari komputer Anda:

**b. Web UI**

```
http://127.0.0.1:4000/ui
```

**c. curl**

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"pribadi-pro","messages":[{"role":"user","content":"halo"}]}'
```

Daftar alias:

```bash
curl http://127.0.0.1:4000/v1/models -H "Authorization: Bearer $MASTER_KEY"
```

**d. Dipakai Hermes / klien OpenAI apa pun**

```
base_url : http://127.0.0.1:4000/v1
api_key  : <MASTER_KEY>
model    : pribadi-pro | pribadi-jenius | pribadi-hemat | pribadi-hemat-2
```

**e. Health check**

```bash
curl http://127.0.0.1:4000/health/liveliness
```

## 8. Troubleshooting

| Gejala | Periksa |
|---|---|
| `install.sh` berhenti setelah membuat `.env` | Normal (langkah 1/2). Isi kunci di `.env`, lalu jalankan ulang. |
| Health check tidak pernah OK | `docker compose logs --tail 50 proxy` — biasanya `GEMINI_KEY`/`OPENROUTER_KEY` kosong atau salah. |
| `401 Unauthorized` dari proxy | `MASTER_KEY` di `.env` berubah setelah container jalan. `docker compose restart proxy`. |
| Semua permintaan 429 | Kuota tier gratis habis / rpm di config terlalu tinggi. Turunkan `rpm` di `litellm-config.yaml`, lalu `docker compose restart proxy`. |
| Model tidak ditemukan | Pastikan alias persis: `pribadi-pro`, `pribadi-jenius`, `pribadi-hemat`, `pribadi-hemat-2`. |
| Bot diam saja | Chat id Anda belum ada di `ALLOWED_CHATS`, atau token salah: `docker compose logs -f bot`. |
| Bot hidup tetapi jawaban error | `docker compose exec bot sh -c 'echo $LITELLM_URL'` harus `http://proxy:4000`; cek `docker compose logs proxy`. |
| Bot pemeriksa tautan diam saja | Chat id belum ada di `ALLOWED_CHATS`, atau `docker compose -f docker-compose.yml -f docker-compose.link.yml logs -f linkbot`. |
| Bot error 409 Conflict | Dua proses memakai token yang sama. Buat bot kedua di @BotFather, isi `LINKBOT_TOKEN`, lalu jalankan ulang `bash install-link.sh`. |
| Tautan tidak bisa diperiksa | Situs memblokir bot pratinjau (403/429) atau melarangnya lewat `robots.txt` — itu memang perilaku yang diinginkan. |
| Perubahan config tidak berlaku | Config di-mount read-only; setelah mengubah file: `docker compose restart proxy`. |
| Cache terasa "menjawab lama" | Redis menyimpan jawaban identik; tunggu TTL (24 jam) atau `docker compose exec redis redis-cli FLUSHALL` (perhatian: hanya milik Anda sendiri). |
| Port 4000 tidak bisa diakses dari luar | Memang by design. Pakai SSH tunnel atau `ssh -L`. Jangan publikasikan port tanpa TLS + firewall. |

## 9. Update

```bash
cd ~/personal-gateway
git pull --ff-only && bash install.sh
```

`install.sh` aman dijalankan berulang: ia tidak menimpa `.env` yang sudah ada dan tidak menghapus volume Redis.

## 10. Struktur file

```
.
├── docker-compose.yml        # proxy (LiteLLM) + redis, port hanya 127.0.0.1:4000
├── litellm-config.yaml       # alias model, fallback, cooldown, cache Redis
├── docker-compose.tele.yml   # overlay bot Telegram AI (opsional)
├── docker-compose.link.yml   # overlay bot pemeriksa tautan (opsional)
├── install.sh                # installer untuk VPS
├── install-tele.sh           # installer add-on bot AI
├── install-link.sh           # installer add-on bot pemeriksa tautan
├── telegram-bot/
│   ├── bot.py                # bot long-polling, stdlib only
│   └── Dockerfile            # python:3.12-slim
├── link-tools/               # bot pemeriksa tautan
│   ├── linkinspect.py        # inti: pengalihan, oEmbed, Open Graph, anti-SSRF
│   ├── bot.py                # bot long-polling Telegram
│   ├── selftest.py           # uji mandiri offline (server HTTP tiruan)
│   └── Dockerfile            # python:3.12-slim
├── .env.example              # contoh konfigurasi (nilai kosong)
├── .gitignore                # .env, data/, cache/, *.log, __pycache__
├── LICENSE                   # MIT
└── README.md
```

## 11. Catatan keamanan

- Proxy **hanya** bind ke `127.0.0.1:4000`. Jangan ubah ke `0.0.0.0` tanpa reverse proxy + TLS + firewall.
- Satu `MASTER_KEY` dipakai untuk semua pemakaian; simpan baik-baik dan jangan pernah di-commit.
- Fitur multi-user/billing dimatikan (`disable_spend_logs`, tanpa `DATABASE_URL`), jadi tidak ada
  virtual key dan tidak ada pencatatan biaya.
- File `.env` dibuat dengan mode `600` oleh installer.

## Lisensi

MIT — lihat [LICENSE](LICENSE).
