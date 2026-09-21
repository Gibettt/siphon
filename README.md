# Siphon

**Download seluruh kode frontend dari website manapun.**

Siphon menganalisis website, mendeteksi framework yang digunakan (React, Next.js, Vue, Angular, dll), lalu mendownload semua file frontend — HTML, CSS, JavaScript, gambar, font — dan mengemasnya dalam satu file ZIP.

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## Fitur

- **Deteksi Framework Otomatis** — Mengenali React, Next.js, Vue, Nuxt, Angular, Svelte, WordPress, Gatsby, Astro, jQuery, Bootstrap
- **3 Mode Download:**

  | Mode | Kegunaan |
  |------|----------|
  | **Normal** | Download halaman & aset standar (HTML, CSS, JS, gambar) |
  | **Deep** | Untuk website 3D/WebGL — pakai Playwright browser untuk capture aset dinamis, WASM, dan konten yang di-render JavaScript |
  | **Complete** | Untuk e-commerce & SPA — ambil konten POST-rendered, semua webpack chunk lazy-load, dan aset dari CSS |

- **Output ZIP siap pakai** — Buka langsung di browser tanpa server
- **Live progress log** — Pantau proses download secara real-time
- **Web UI** — Tidak perlu command line, cukup buka browser

## Cara Pakai

### 1. Clone & Install

```bash
git clone https://github.com/Gibettt/siphon.git
cd siphon
pip install -r requirements.txt
```

### 2. Install Playwright (untuk Deep Mode)

```bash
playwright install chromium
```

### 3. Jalankan

```bash
./run.sh
```

atau manual:

```bash
python3 -m uvicorn main:app --host 0.0.0.0 --port 8765
```

### 4. Buka Browser

```
http://localhost:8765
```

Masukkan URL website → pilih mode → klik Download. Selesai.

## Kapan Pakai Mode Apa?

| Situasi | Mode |
|---------|------|
| Website biasa, blog, landing page | **Normal** |
| NASA Eyes, Sketchfab, Three.js, WebGL app | **Deep** |
| Shopify, Salesforce, website dengan webpack/lazy-load | **Complete** |

## Struktur Project

```
siphon/
├── main.py              # Server FastAPI + API endpoints
├── crawler.py            # Crawler mode Normal
├── deep_crawler.py       # Crawler mode Deep (Playwright)
├── complete_crawler.py   # Crawler mode Complete (POST + webpack)
├── detector.py           # Deteksi framework frontend
├── templates/
│   └── index.html        # Halaman web UI
├── static/
│   ├── style.css         # Stylesheet
│   └── app.js            # Client-side logic
├── requirements.txt
└── run.sh                # Script untuk menjalankan server
```

## Requirements

- Python 3.10+
- Chromium (hanya untuk Deep Mode)

## API Endpoints

| Method | Endpoint | Fungsi |
|--------|----------|--------|
| `POST` | `/api/crawl` | Mulai download website |
| `GET` | `/api/job/{id}` | Cek status & progress |
| `GET` | `/api/download/{id}` | Download file ZIP hasil |

### Contoh Request

```bash
curl -X POST http://localhost:8765/api/crawl \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "max_pages": 10}'
```

## License

MIT
