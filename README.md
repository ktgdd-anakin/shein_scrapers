# shein_scrapers

Playwright-based SHEIN US product-page scraper.  
Saves each PDP as `pages/<product_id>.html` + `pages/<product_id>.availability.json`.

---

## Folder layout

```
shein_scrapers/
├── url_clicker.py          # main entry point
├── shein_captcha.py        # captcha detection & solver
├── shein_variants.py       # color-variant PDP scraper
├── proxy_sessions.py       # proxy state, cooldown, helpers
├── scrape_state.py         # SQLite progress tracking
├── PLP_shein/              # category-page (PLP) scraper helpers
├── pids.txt                # product ids to scrape (one per line or JSON array)
├── geonode_proxies.txt     # your proxy list (one URL per line)
├── geonode_proxies.example.txt
├── requirements.txt
└── README.md
```

---

## Setup

```bash
# 1 — Python 3.11+
python3 --version

# 2 — create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3 — install dependencies
pip install -r requirements.txt

# 4 — download Chromium
playwright install chromium
```

---

## Proxies

Create `geonode_proxies.txt` — one Geonode sticky-session URL per line:

```
http://user-username_session-abc:password@premium.geonode.com:9000
http://user-username_session-def:password@premium.geonode.com:9000
```

Use `geonode_proxies.example.txt` as a template.  
Each proxy gets a **1-hour cooldown** after its session ends or is blocked.

---

## Product IDs

Put the Shein `goods_id` values you want to scrape in `pids.txt`, one per line:

```
433580538
432958053
433920826
```

---

## Running

### Basic — manual captcha solving

```bash
python3 url_clicker.py
```

- Opens **4 parallel sessions** (default), each with **2 tabs**.
- First navigation per session uses **1 tab** (warmup).
- If a **captcha** appears: solve it in the Chromium window, then **press Enter** in the terminal — the extra tabs open automatically once the warmup URL succeeds.
- Use `--resume` to skip already-scraped products on restart.

### Recommended for manual captcha solving (one window, easier to manage)

```bash
python3 url_clicker.py --session-slots 1 --tabs 1
```

### Auto captcha solving (OpenAI GPT-4o vision)

```bash
export OPENAI_API_KEY=sk-...
python3 url_clicker.py
# or inline:
python3 url_clicker.py --openai-key sk-...
```

Auto-solve tries the checkbox + image-sequence challenge automatically.  
Falls back to manual Enter prompt if it cannot solve it.

### Save a warm browser session first (reduces captcha frequency)

```bash
# Opens a headed browser, you solve any captcha once, cookies are saved
python3 url_clicker.py --prime-browser-state

# Then reuse those cookies on every proxy session
python3 url_clicker.py --browser-state shein_browser_state.json
```

---

## Common options

| Flag | Default | Description |
|------|---------|-------------|
| `--session-slots N` | 4 | Parallel proxy sessions |
| `--tabs N` | 2 | Tabs per session (max 4) |
| `--resume` | off | Skip already-done URLs |
| `--limit N` | all | Max URLs this run |
| `--no-variants` | off | Skip color-variant PDPs |
| `--headless` | off | Run Chromium headless (no window) |
| `--openai-key KEY` | — | GPT-4o key for auto captcha solve |
| `--browser-state FILE` | — | Reuse saved session cookies |
| `--prime-browser-state` | — | Solve captcha once, save cookies |
| `--pause-first SEC` | 0 | Wait SEC seconds before first nav |
| `--url-source urls` | pids | Read full URLs from `urls` file instead of building from `pids.txt` |
| `--scrape-state-db PATH` | `scrape_state.sqlite3` | SQLite progress DB |
| `--prune-scraped` | — | Remove done URLs from `urls` file |

Run `python3 url_clicker.py --help` for the full list.

---

## Output files

| File | Description |
|------|-------------|
| `pages/<id>.html` | Saved PDP HTML |
| `pages/<id>.availability.json` | Stock / sold-out from `gbRawData` |
| `pages/<product_key>.html` | Color-variant PDPs (default on) |
| `log.csv` | Per-URL outcome log (`ok`, `blocked`, `nav_error`, …) |
| `progress.txt` | Successfully scraped URLs (used with `--resume`) |
| `scrape_state.sqlite3` | Full SQLite state (outcomes + discovered pids) |
| `proxy_state.json` | Per-proxy cooldown times |
| `captcha_debug/` | Captcha screenshots (set `SHEIN_CAPTCHA_DEBUG=1`) |

---

## Resuming after interruption

```bash
python3 url_clicker.py --resume
```

Already-scraped products are skipped based on `pages/<id>.html` on disk,  
`log.csv` ok rows, SQLite records, and (with `--resume`) `progress.txt`.

---

## Environment variables

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | OpenAI key for image-seq captcha auto-solve |
| `SHEIN_CAPTCHA_DEBUG=1` | Save captcha screenshots to `captcha_debug/` |
| `CHROME_CDP_URL` | Override CDP address (default `http://127.0.0.1:9222`) |
