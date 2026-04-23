"""
url_clicker.py
==============
Default mode: Playwright Chromium with **incognito** (ephemeral) browser
contexts, one **Geonode sticky proxy** per session. Each session runs up to
**10 minutes** (or until a login page), with **no fixed cap** on how many
PDPs it can finish in that window (default: product ids in ``pids.txt``,
turned into Shein PDP URLs; or one URL per line in ``urls`` with
``--url-source=urls``); then that proxy is stored in
``proxy_state.json`` and is **not reused for 1 hour**. If a **login** page is
detected, the session stops and the same 1-hour cooldown applies.

**Proxy file** (`geonode_proxies.txt`): one proxy URL per line, e.g.
``http://user:pass@gateway.geonode.io:9000``.

**CDP mode** (optional): connect to your Chrome with ``--cdp`` and use
``--setup`` once as before.

Chrome CDP setup (only needed with `--cdp`)
--------------------------------------
Chrome only opens DevTools on port 9222 when ``--user-data-dir`` is **not**
the normal ``~/Library/.../Google/Chrome`` folder. This script uses
``chrome_cdp_profile/`` and **symlinks** your real profile into it.

    python3 url_clicker.py --setup

Wait until you see “CDP is listening”, then run with ``--cdp``.

RUN
---
    python3 url_clicker.py                # proxy mode; queue from ./pids.txt (built Shein PDP URLs)
    python3 url_clicker.py --session-slots 4   # optional: parallel proxy sessions (default 4)
    python3 url_clicker.py --url-source urls   # read one URL per line from ./urls (legacy)
    python3 url_clicker.py --tabs 2       # optional; default 2 tabs per session (max 4)
    python3 url_clicker.py --cdp          # attach to Chrome on port 9222
    python3 url_clicker.py --prime-browser-state   # solve captcha once, save shein_browser_state.json
    python3 url_clicker.py --browser-state shein_browser_state.json   # reuse that session
    python3 url_clicker.py --limit 100    # first 100 unique queue entries
    python3 url_clicker.py --resume       # skip already-done URLs
    python3 url_clicker.py --no-variants  # skip other color PDPs (on by default)
    python3 url_clicker.py --pause-first 60 --limit 1   # wait 60s after browser opens (watch first run)

OUTPUT
------
    pages/          HTML files saved as <product_id>.html
                    + <product_id>.availability.json (stock / sold_out from gbRawData when parseable)
    Color variant PDPs (default on): extra pages as pages/<product_key>.html (allColorDetailImages keys)
    progress.txt    (with --resume) last-success lines; work queue also skips by product id
                    from pages/<id>.html, log.csv (ok), and SQLite when --scrape-state-db is on
    log.csv         product_id | url | status | saved_file | proxy
    scrape_state.sqlite3   SQLite (WAL): ok / errors per URL; ``discovered`` pids from data-id
                    (carousels / “customers also viewed”) for later runs; checkpoint every ~7 min
    proxy_state.json   per-proxy cooldown (proxy mode)
    geonode_proxies.txt   proxy list (you create this)

PRUNE (remove finished work from urls)
---------------------------------------
    python3 url_clicker.py --prune-scraped

Rewrites ``urls`` to drop any URL already recorded in ``pages/``,
``progress.txt``, or ``log.csv`` (status=ok).  Backs up the old file as
``urls.bak``.  Run this **before** deleting progress/log/pages if you want
to shrink the list.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from collections import deque
import csv
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import random
import time
import zlib
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

from PLP_shein.plp_html_pids import extract_product_ids_from_plp_html
from scrape_state import RETRYABLE_STATUSES, ScrapeState
from shein_captcha import auto_solve_captcha
from shein_variants import (
    scrape_variant_matrix,
    shein_pdp_parseable_gb_raw_data,
    write_availability_json,
)
from proxy_sessions import (
    MAX_TABS_PER_SESSION,
    ProxyState,
    SESSION_WORK_SEC,
    check_login_page,
    load_proxy_lines,
    mask_proxy_url,
    parse_playwright_proxy,
    pick_next_proxy,
    wait_seconds_for_next_proxy,
)

# ── paths ──────────────────────────────────────────────────────────────────
BASE        = Path(__file__).parent
URLS_FILE   = BASE / "urls"
PIDS_FILE   = BASE / "pids.txt"
SCRAPE_STATE_DB = BASE / "scrape_state.sqlite3"
PAGES_DIR   = BASE / "pages"
PROGRESS    = BASE / "progress.txt"
LOG_CSV     = BASE / "log.csv"
PROXY_FILE_DEFAULT = BASE / "geonode_proxies.txt"
# Playwright storage_state JSON: cookies + origins (localStorage). See --prime-browser-state.
DEFAULT_BROWSER_STATE_FILE = BASE / "shein_browser_state.json"
# Use 127.0.0.1 — on macOS, "localhost" can resolve to ::1 while Chrome binds IPv4 only.
CDP         = os.environ.get("CHROME_CDP_URL", "http://127.0.0.1:9222")
CHROME      = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CDP_HOST    = "127.0.0.1"
CDP_PORT    = 9222
# Real Chrome data (Dock / normal launch).  Do NOT pass this as --user-data-dir
# with --remote-debugging-port — Chrome refuses to open DevTools and logs:
#   "DevTools remote debugging requires a non-default data directory"
CHROME_USER_DATA = Path.home() / "Library/Application Support/Google/Chrome"
# Separate folder for CDP — must differ from CHROME_USER_DATA.
CDP_USER_DATA_DIR = BASE / "chrome_cdp_profile"
# Second profile in picker (“Dileep” / “Dileep K”) → Chrome folder "Default".
# First tile ("anakin.company") → "Profile 1".
DEFAULT_PROFILE_DIR = "Default"

# ── captcha keywords that trigger a pause ──────────────────────────────────
CAPTCHA_SIGNALS = [
    "verify you are human",
    "i am human",
    "please select the following",
    "verify-wrap",
    "captcha-verify",
    "risk/challenge",
]

# Realistic desktop Chrome fingerprint (proxy mode) — reduces generic bot blocks.
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# First SHEIN page is slow (cold start, trackers, etc.); later pages are ~10–15 s.
NAV_TIMEOUT_FIRST_MS = 120_000   # 2 min
NAV_TIMEOUT_REST_MS = 30_000       # 10–15 s typical + buffer
# After domcontentloaded: let carousels / “Customers also viewed” / BTF widgets hydrate
# Dwell after domcontentloaded; override with --pdp-dwell-*.
PDP_PAGE_DWELL_MIN_DEFAULT = 8.0
PDP_PAGE_DWELL_MAX_DEFAULT = 12.0
DEFAULT_PROXY_TABS = 2
# Concurrent proxy *sessions* (separate incognito context + one sticky proxy each) in one process.
DEFAULT_SESSION_SLOTS = 4
POST_NAV_SETTLE_FIRST = (2.0, 5.0)  # legacy (random PDP dwell is used after domcontentloaded)
POST_NAV_SETTLE_REST = (0.4, 0.9)
DELAY_AFTER_SAVE_FIRST = (3.0, 6.0)   # pause before next URL
DELAY_AFTER_SAVE_REST = (2.0, 5.0)
# Periodic WAL sync for scrape_state.sqlite3 (multi-session safe)
STATE_SYNC_SEC_DEFAULT = 420.0

# PDP URL pattern (see shein_url_builder.py): one fixed path segment + ``-p-{goods_id}.html``;
# SHEIN uses the ``-p-`` id for routing; other slug text is often accepted.
SHEIN_PDP_SLUG = (
    "Women-Button-Down-Shirt-And-Wide-Leg-Pants-Two-Piece-Outfit-Casual-Linen-Set"
)
# Same query string as in shein_url_builder.build_shein_url; omit for shorter links / --strip-url-query
SHEIN_PDP_QUERY = (
    "?src_identifier=fc%3DWomen%20Clothing%60sc%3DWomen%20Clothing%60tc%3D0%60oc%3D0%60ps%3D"
    "tab03navbar03%60jc%3DitemPicking_017172961&src_module=topcat&src_tab_page_id=page_"
    "otherundefined&mallCode=1&pageListType=4&imgRatio=3-4&detailBusinessFrom=0-1_"
    "433588815%7C0-2&pageListType=4"
)


def build_shein_pdp_url(product_id: str | int, *, include_query: bool = True) -> str:
    """
    Build a US Shein product URL for ``product_id`` (goods id as string or int).
    Reference: shein_url_builder.build_shein_url — same path + query template, id only in ``-p-``.
    """
    pid = str(product_id).strip()
    path = f"https://us.shein.com/{SHEIN_PDP_SLUG}-p-{pid}.html"
    if not include_query:
        return path
    return path + SHEIN_PDP_QUERY


def collect_done_set(resume: bool, st: "ScrapeState | None") -> set[str]:
    """
    Set of URL string variants to treat as “done” (exact + with/without query per pid).
    Prefer :func:`url_already_scraped` for queue filtering.
    """
    done_urls, done_pids = collect_done_tracking(resume, st)
    out = set(done_urls)
    for p in done_pids:
        if p.isdigit():
            out.add(build_shein_pdp_url(p, include_query=True))
            out.add(build_shein_pdp_url(p, include_query=False))
    return out


def collect_done_tracking(
    resume: bool,
    st: "ScrapeState | None",
) -> tuple[set[str], set[str]]:
    """
    Return ``(done_urls, done_pids)`` for skipping work already finished.

    * **done_urls** — exact strings (``progress.txt`` when ``resume``, ``log.csv`` ok rows,
      SQLite ok URLs).
    * **done_pids** — Shein ``-p-<goods_id>`` strings from ``pages/<id>.html`` on disk
      (numeric stems only), ok rows in the log, SQLite, and pids parsed from progress lines.

    Queue URLs are skipped if the exact URL is in ``done_urls`` *or* the product id is in
    ``done_pids`` (handles query-stripped vs full template URLs and other variants).
    """
    pids, exact = _collect_scraped_ids_and_urls(include_progress=resume)
    if st is not None:
        exact |= st.success_urls()
        pids |= st.success_pids()
    return exact, pids


def url_already_scraped(
    url: str,
    done_urls: set[str],
    done_pids: set[str],
) -> bool:
    """True if this URL is done by exact match or by goods_id in ``done_pids``."""
    if url in done_urls:
        return True
    pid = _product_id(url)
    return bool(pid.isdigit() and pid in done_pids)


def ingest_pdp_recommendation_pids(
    html: str, current_pid: str, current_url: str, st: "ScrapeState | None"
) -> int:
    """
    Pids from ``data-id`` (and href fallback) in the saved PDP, e.g. carousels.
    Excludes the current product; queues new pids in SQLite for later runs.
    """
    if st is None:
        return 0
    pids = extract_product_ids_from_plp_html(html)
    return st.add_discovered_pids(
        pids, from_pid=current_pid, from_url=current_url
    )


async def periodic_state_sync(
    st: "ScrapeState", stop: asyncio.Event, interval_sec: float
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=interval_sec
            )
            break
        except asyncio.TimeoutError:
            st.wal_checkpoint()
            s = st.summary()
            print(
                f"\n[state] checkpoint  |  ok={s['ok']}  errors={s['not_ok']}  "
                f"discovered={s['discovered_pending']}  db={s['db']}",
                flush=True,
            )


def load_pids_from_file(path: Path) -> list[str]:
    """
    Read product ids from ``pids.txt``-style file: a JSON array of numbers, or
    one id per line (commas optional).
    """
    if not path.exists():
        sys.exit(f"[error] pids file not found: {path}")
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return []
    if text.lstrip().startswith("["):
        data = json.loads(text)
        if not isinstance(data, list):
            sys.exit(f"[error] {path.name} must be a JSON array of product ids, got {type(data).__name__}")
        return [str(x).strip() for x in data if str(x).strip().isdigit()]
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line or line in "[]{}":
            continue
        if line.isdigit():
            out.append(line)
    return out


def load_url_lines_from_file(path: Path, limit: int | None) -> list[str]:
    if not path.exists():
        sys.exit(f"[error] '{path}' not found.")
    seen, out = set(), []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            url = line.strip()
            if url and url not in seen:
                seen.add(url)
                out.append(url)
                if limit and len(out) >= limit:
                    break
    print(
        f"[loader] {len(out)} unique URL(s) read from {path.name}.",
        flush=True,
    )
    return out


def sample_pdp_dwell_sec(pmin: float, pmax: float) -> float:
    """Random dwell in ``[pmin, pmax]`` (or ``pmin`` when pmax <= pmin)."""
    if pmax <= pmin:
        return float(pmin)
    return random.uniform(pmin, pmax)


def _retry_url_belongs_to_worker_shard(
    url: str, worker_index: int | None, worker_total: int | None
) -> bool:
    """Stable assignment so each parallel run only requeues a subset of retry URLs."""
    if worker_index is None or worker_total is None:
        return True
    c = zlib.crc32(url.encode("utf-8", errors="replace")) & 0xFFFFFFFF
    return (c % worker_total) == worker_index


def merge_retry_urls_into(
    out: list[str],
    scrape_state: "ScrapeState | None",
    worker_index: int | None = None,
    worker_total: int | None = None,
) -> None:
    """
    Append URLs that failed with a retryable status (see ``RETRYABLE_STATUSES``)
    so the next run picks them up again. Dedupes by URL. When ``--worker`` / ``--workers``
    is used, only retry rows assigned to this worker are appended (stable hash of URL).
    """
    if scrape_state is None:
        return
    retry = scrape_state.urls_pending_retry()
    if not retry:
        return
    seen: set[str] = set(out)
    n = 0
    for u in retry:
        if not _retry_url_belongs_to_worker_shard(u, worker_index, worker_total):
            continue
        if u not in seen:
            out.append(u)
            seen.add(u)
            n += 1
    if n:
        st_list = ", ".join(sorted(RETRYABLE_STATUSES))
        print(
            f"[loader] +{n} URL(s) from scrape DB to retry (last status in {{{st_list}}}).",
            flush=True,
        )


def load_work_queue(
    limit: int | None,
    *,
    url_source: str = "pids",
    pids_path: Path = PIDS_FILE,
    urls_path: Path = URLS_FILE,
    scrape_state: "ScrapeState | None" = None,
    worker_index: int | None = None,
    worker_total: int | None = None,
) -> list[str]:
    """
    List of unique PDP URLs: either built from pids (default) or read from
    a legacy one-URL-per-line file (``urls``). When ``scrape_state`` is set,
    appends pids from prior recommendation discovery (``discovered`` table) and
    URLs that last failed with a retryable status (``urls_pending_retry``).
    With ``--worker`` / ``--workers``, only rows ``i`` where ``i % workers == worker``
    are kept (for parallel runs).
    """
    if (worker_index is None) ^ (worker_total is None):
        sys.exit("[error] --worker and --workers must be used together.")
    if worker_total is not None and worker_total < 1:
        sys.exit("[error] --workers must be >= 1.")
    if worker_index is not None and (
        worker_index < 0 or worker_index >= worker_total
    ):
        sys.exit(f"[error] --worker must be from 0 to {worker_total - 1}.")

    if url_source == "pids":
        pids = list(dict.fromkeys(load_pids_from_file(pids_path)))
        n_extra = 0
        if scrape_state is not None:
            for p in scrape_state.pending_scrape_pids():
                if p not in pids:
                    pids.append(p)
                    n_extra += 1
            if n_extra:
                print(
                    f"[loader] +{n_extra} product id(s) from scrape DB (recommendations / carousels).",
                    flush=True,
                )
        if worker_index is not None:
            n_before = len(pids)
            pids = [
                p
                for i, p in enumerate(pids)
                if i % worker_total == worker_index
            ]
            print(
                f"[loader] worker {worker_index}/{worker_total} shard → "
                f"{len(pids)} id(s) (of {n_before} before shard).",
                flush=True,
            )
        if limit is not None:
            pids = pids[:limit]
        out = [build_shein_pdp_url(pid) for pid in pids]
        merge_retry_urls_into(
            out, scrape_state, worker_index, worker_total
        )
        print(
            f"[loader] {len(out)} PDP URL(s) built from {pids_path.name} "
            f"({SHEIN_PDP_SLUG[:24]}…-p-<id>.html; use --url-source=urls to use a url list).",
            flush=True,
        )
        return out
    out = load_url_lines_from_file(urls_path, None)
    if worker_index is not None:
        n_before = len(out)
        out = [
            u
            for i, u in enumerate(out)
            if i % worker_total == worker_index
        ]
        print(
            f"[loader] worker {worker_index}/{worker_total} shard → "
            f"{len(out)} URL(s) (of {n_before} before shard).",
            flush=True,
        )
    if limit is not None:
        out = out[:limit]
    merge_retry_urls_into(
        out, scrape_state, worker_index, worker_total
    )
    return out


def _cdp_port_open() -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.4)
        s.connect((CDP_HOST, CDP_PORT))
        s.close()
        return True
    except OSError:
        return False


def _cdp_http_ok() -> bool:
    """Chrome CDP serves /json/version over HTTP when the port is ready."""
    for base in (f"http://{CDP_HOST}:{CDP_PORT}", "http://[::1]:9222"):
        try:
            with urllib.request.urlopen(f"{base}/json/version", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            continue
    return False


def wait_for_cdp(timeout_s: float = 90.0, label: str = "") -> bool:
    """Block until something listens on CDP_PORT or timeout."""
    deadline = time.monotonic() + timeout_s
    first = True
    try:
        while time.monotonic() < deadline:
            if _cdp_http_ok() or _cdp_port_open():
                return True
            if first and label:
                print(label, flush=True)
                first = False
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nInterrupted (Ctrl+C).", flush=True)
        raise SystemExit(130) from None
    return False


# ───────────────────────────────────────────────────────────────────────────
# helpers
# ───────────────────────────────────────────────────────────────────────────

def _notify(msg: str):
    """macOS system notification + terminal bell."""
    print(f"\a\n{'!'*60}\n  {msg}\n{'!'*60}\n")
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{msg}" with title "url_clicker" sound name "Basso"'],
            capture_output=True
        )
    except Exception:
        pass


def _product_id(url: str) -> str:
    m = re.search(r"-p-(\d+)\.html", url)
    return m.group(1) if m else re.sub(r"[^\w]", "_", url)[-40:]


def _collect_scraped_ids_and_urls(
    *, include_progress: bool = True
) -> tuple[set[str], set[str]]:
    """
    Product IDs and exact URLs already finished (``pages/``, optional ``progress.txt``,
    ``log.csv`` ok rows). Only numeric ``pages/*.html`` stems count as product ids
    (skips ad-hoc saved HTML with non-numeric names).
    """
    pids: set[str] = set()
    exact: set[str] = set()

    if PAGES_DIR.exists():
        for p in PAGES_DIR.glob("*.html"):
            if p.stem.isdigit():
                pids.add(p.stem)

    if include_progress and PROGRESS.exists():
        for line in PROGRESS.read_text(encoding="utf-8", errors="replace").splitlines():
            u = line.strip()
            if not u:
                continue
            exact.add(u)
            m = re.search(r"-p-(\d+)\.html", u)
            if m:
                pids.add(m.group(1))

    if LOG_CSV.exists():
        with LOG_CSV.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("status") != "ok":
                    continue
                pid = (row.get("product_id") or "").strip()
                if pid:
                    pids.add(pid)
                u = (row.get("url") or "").strip()
                if u:
                    exact.add(u)
                    m = re.search(r"-p-(\d+)\.html", u)
                    if m:
                        pids.add(m.group(1))

    return pids, exact


def prune_scraped_from_urls_file() -> None:
    """
    Rewrite ``urls`` without lines that are already scraped / logged / saved.
    """
    if not URLS_FILE.exists():
        sys.exit(f"[prune] '{URLS_FILE}' not found.")

    pids, exact_urls = _collect_scraped_ids_and_urls()
    if not pids and not exact_urls:
        print(
            "[prune] No data in pages/, progress.txt, or log.csv (ok rows).\n"
            "        Nothing removed. Restore those files from backup if you need to prune."
        )
        return

    bak = BASE / "urls.bak"
    n = 0
    while bak.exists():
        n += 1
        bak = BASE / f"urls.bak.{n}"

    shutil.copy2(URLS_FILE, bak)
    print(f"[prune] Backup → {bak}")

    kept, dropped = 0, 0
    out_lines: list[str] = []
    with URLS_FILE.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            u = line.strip()
            if not u:
                continue
            if u in exact_urls:
                dropped += 1
                continue
            pid = _product_id(u)
            if pid in pids:
                dropped += 1
                continue
            out_lines.append(line if line.endswith("\n") else line + "\n")
            kept += 1

    URLS_FILE.write_text("".join(out_lines), encoding="utf-8")
    print(f"[prune] Removed {dropped} URL(s), kept {kept}.  Scraped keys: {len(pids)} product id(s).")


def load_urls(limit: int | None) -> list[str]:
    """Deprecated name: use :func:`load_work_queue` with ``url_source='urls'``."""
    return load_work_queue(
        limit, url_source="urls", urls_path=URLS_FILE
    )


def load_progress() -> set[str]:
    if not PROGRESS.exists():
        return set()
    with PROGRESS.open() as f:
        return {l.strip() for l in f if l.strip()}


def mark_done(url: str):
    with PROGRESS.open("a") as f:
        f.write(url + "\n")


def open_log(resume: bool):
    write_header = not LOG_CSV.exists() or not resume
    fh = LOG_CSV.open("a", newline="", encoding="utf-8")
    w  = csv.writer(fh)
    if write_header:
        w.writerow(["product_id", "url", "status", "saved_file", "proxy"])
    return fh, w


def _nav_error_hint(exc: Exception) -> str:
    """Extra context for common Chromium proxy / WAF failures."""
    msg = str(exc).lower()
    if "err_http_response_code_failure" in msg or "http_response_code" in msg:
        return (
            "  [hint] Often HTTP 403/503 from the site or proxy. Try: "
            "--chrome (real Chrome), residential Geonode, --strip-url-query, "
            "or a shorter /p-XXXX.html link."
        )
    if "err_tunnel" in msg or "tunnel_connection_failed" in msg:
        return (
            "  [hint] HTTPS tunnel (CONNECT) through the proxy failed — usually wrong "
            "Geonode **HTTP** port (dashboard; often 10000–10900), SOCKS port used by "
            "mistake, bad credentials, or proxy unreachable. Test: "
            "python3 test_geonode_proxy.py --from-env"
        )
    if "net::err_proxy" in msg or ("proxy" in msg and "failed" in msg):
        return "  [hint] Check GEONODE_PORT, credentials, and proxy plan."
    return ""


def save_html(pid: str, html: str) -> str:
    PAGES_DIR.mkdir(exist_ok=True)
    path = PAGES_DIR / f"{pid}.html"
    path.write_text(html, encoding="utf-8")
    return str(path)


# ───────────────────────────────────────────────────────────────────────────
# captcha detection
# ───────────────────────────────────────────────────────────────────────────

async def check_captcha(page) -> bool:
    """Return True if a captcha is visible on the current page."""
    # 1. check the URL
    if "risk/challenge" in page.url:
        return True
    # 2. check page text / DOM for known captcha signals
    try:
        content = (await page.content()).lower()
        return any(sig in content for sig in CAPTCHA_SIGNALS)
    except Exception:
        return False


async def wait_for_captcha_solve(page, tab_label: str | None = None):
    """
    Pause and wait for the user to manually solve the captcha in Chrome,
    then press Enter in this terminal to resume.
    """
    where = f" (tab {tab_label})" if tab_label else ""
    _notify(
        f"CAPTCHA detected{where}!  Solve it in that Chromium tab, then press Enter here."
    )
    prompt = (
        f"  >> Tab {tab_label}: Press Enter after solving captcha: "
        if tab_label
        else "  >> Press Enter after solving captcha: "
    )
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, input, prompt)
    await asyncio.sleep(1.5)  # brief settle after solve


async def run_prime_browser_state(
    out_path: Path,
    *,
    proxy_file: Path,
    use_chrome: bool = False,
) -> None:
    """
    One headed browser session to us.shein.com: you solve any challenge, then we
    save :func:`playwright.storage_state` for ``--browser-state`` on later runs.
    Uses the first proxy in ``proxy_file`` when available so cookies match that IP.
    """
    print(
        "[prime] Headed browser — solve any challenge in the window; Enter in the terminal when done.\n"
        f"[prime] State file: {out_path}",
        flush=True,
    )
    proxies = load_proxy_lines(proxy_file)
    pw_proxy = None
    if proxies:
        try:
            pw_proxy = parse_playwright_proxy(proxies[0])
            print(
                f"[prime] Using first proxy from {proxy_file.name} (same IP as a typical first session).",
                flush=True,
            )
        except ValueError as exc:
            print(
                f"[prime] Skipping proxy ({exc}); using direct connection.",
                flush=True,
            )
    launch_kw: dict = {
        "headless": False,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if use_chrome:
        launch_kw["channel"] = "chrome"
    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kw)
        context = None
        try:
            ctx_kw: dict = {
                "ignore_https_errors": True,
                "user_agent": CHROME_USER_AGENT,
                "locale": "en-US",
                "timezone_id": "America/New_York",
                "viewport": {"width": 1365, "height": 900},
                "extra_http_headers": {
                    "Accept-Language": "en-US,en;q=0.9",
                },
            }
            if pw_proxy is not None:
                ctx_kw["proxy"] = pw_proxy
            context = await browser.new_context(**ctx_kw)
            page = await context.new_page()
            await page.goto(
                "https://us.shein.com/",
                wait_until="domcontentloaded",
                timeout=120_000,
            )
            n = 0
            while await check_captcha(page):
                n += 1
                print(
                    f"[prime] Still seeing a challenge (step {n}). Solve in the browser, "
                    "then press Enter here.",
                    flush=True,
                )
                await wait_for_captcha_solve(page)
            await asyncio.sleep(1.5)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(out_path))
            print(f"\n[prime] Saved → {out_path}", flush=True)
            print(
                f"[prime] Scrape with:  python3 url_clicker.py --browser-state {out_path}",
                flush=True,
            )
        finally:
            if context is not None:
                await context.close()
            await browser.close()


# ───────────────────────────────────────────────────────────────────────────
# setup Chrome
# ───────────────────────────────────────────────────────────────────────────

CHROME_BUNDLE = "/Applications/Google Chrome.app"
CHROME_SETUP_LOG = BASE / "chrome_setup.log"


def _kill_chrome_hard() -> None:
    """Quit every Chrome-related process (main + helpers) so locks release."""
    for args in (
        ["killall", "-9", "Google Chrome"],
        ["killall", "-9", "Google Chrome Helper"],
        ["pkill", "-9", "-f", "Google Chrome.app"],
    ):
        subprocess.run(args, capture_output=True)


def _remove_chrome_singleton_locks(user_data_root: Path) -> None:
    """
    Stale Singleton* files block a fresh start; safe only after Chrome is dead.
    """
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = user_data_root / name
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


def prepare_cdp_user_data(profile_directory: str) -> Path:
    """
    Chrome only enables remote debugging when --user-data-dir is NOT the default
    install path. We use CDP_USER_DATA_DIR and link the chosen profile folder
    (Default = Dileep K, etc.) into it so cookies/logins match your real profile.
    Do not run normal Chrome on that profile at the same time (same files).
    """
    CDP_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    dest = CDP_USER_DATA_DIR / profile_directory
    src = CHROME_USER_DATA / profile_directory
    if not src.is_dir():
        sys.exit(f"[error] Profile folder not found: {src}")
    if not dest.exists():
        try:
            dest.symlink_to(src, target_is_directory=True)
            print(
                f"[setup] Linked profile: {dest} → {src}\n"
                "        (same logins as your normal Chrome for that profile.)"
            )
        except OSError as exc:
            print(f"[setup] Symlink failed ({exc}); copying profile (may take a while) …")
            shutil.copytree(src, dest, symlinks=True)
    return CDP_USER_DATA_DIR


def _chrome_launch_args(profile_directory: str, uds: str) -> list[str]:
    return [
        CHROME,
        f"--user-data-dir={uds}",
        f"--profile-directory={profile_directory}",
        f"--remote-debugging-port={CDP_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def _tail_log(path: Path, max_bytes: int = 6000) -> str:
    if not path.exists():
        return "(no log file)"
    data = path.read_bytes()
    if len(data) > max_bytes:
        data = data[-max_bytes:]
    return data.decode("utf-8", errors="replace")


def setup_chrome(profile_directory: str):
    """
    Launch Chrome with CDP on 9222. Kills stale Chrome, clears singleton locks,
    logs startup to chrome_setup.log, then tries direct binary and (if needed)
    `open -na` which forces a new instance on macOS.
    """
    uds_root = prepare_cdp_user_data(profile_directory)
    uds = str(uds_root)
    print("[setup] Force-quitting Chrome …")
    _kill_chrome_hard()
    time.sleep(5)
    _remove_chrome_singleton_locks(CHROME_USER_DATA)
    _remove_chrome_singleton_locks(uds_root)
    time.sleep(1)

    cmd = _chrome_launch_args(profile_directory, uds)
    print(
        f"[setup] user-data-dir={uds}  (must NOT be default Chrome path — CDP requires this)\n"
        f"[setup] profile-directory={profile_directory!r}  CDP {CDP_HOST}:{CDP_PORT}"
    )
    print(f"[setup] Logging Chrome output → {CHROME_SETUP_LOG}")

    with CHROME_SETUP_LOG.open("wb") as logf:
        subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)

    if wait_for_cdp(120.0, "[setup] Waiting for DevTools port …"):
        print("[setup] OK — CDP is listening. Run:  python3 url_clicker.py --cdp")
        sys.exit(0)

    print("[setup] Direct launch did not open :9222 — trying macOS `open -na` (new instance) …")
    _kill_chrome_hard()
    time.sleep(3)
    _remove_chrome_singleton_locks(CHROME_USER_DATA)
    _remove_chrome_singleton_locks(uds_root)
    time.sleep(1)

    subprocess.run(
        [
            "open",
            "-na",
            CHROME_BUNDLE,
            "--args",
            f"--user-data-dir={uds}",
            f"--profile-directory={profile_directory}",
            f"--remote-debugging-port={CDP_PORT}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        capture_output=True,
    )

    if wait_for_cdp(120.0, "[setup] Waiting for DevTools port …"):
        print("[setup] OK — CDP is listening. Run:  python3 url_clicker.py --cdp")
        sys.exit(0)

    print(
        "[setup] ERROR: port 9222 never opened.\n"
        "  Last lines from chrome_setup.log:\n"
        f"  ---\n{_tail_log(CHROME_SETUP_LOG)}\n  ---\n"
        "  Try:\n"
        "  1) Quit Chrome from the menu, wait 10 s, run --setup again.\n"
        "  2) In Terminal, run the same command printed below and read errors.\n"
        "  3) Open chrome://policy — if remote debugging is blocked by policy, CDP cannot work.\n"
        f'  "{CHROME}" --user-data-dir="{uds}" --profile-directory={profile_directory} '
        f"--remote-debugging-port={CDP_PORT}\n"
    )
    sys.exit(1)


# ───────────────────────────────────────────────────────────────────────────
# main loop
# ───────────────────────────────────────────────────────────────────────────

async def run_cdp(
    limit: int | None,
    resume: bool,
    scrape_variants: bool = True,
    *,
    pause_first_sec: float = 0.0,
    url_source: str = "pids",
    pids_path: Path = PIDS_FILE,
    urls_path: Path = URLS_FILE,
    scrape_state: "ScrapeState | None" = None,
    pdp_dwell_min: float = PDP_PAGE_DWELL_MIN_DEFAULT,
    pdp_dwell_max: float = PDP_PAGE_DWELL_MAX_DEFAULT,
    worker_index: int | None = None,
    worker_total: int | None = None,
    state_sync_sec: float = STATE_SYNC_SEC_DEFAULT,
    openai_api_key: str | None = None,
):
    urls = load_work_queue(
        limit,
        url_source=url_source,
        pids_path=pids_path,
        urls_path=urls_path,
        scrape_state=scrape_state,
        worker_index=worker_index,
        worker_total=worker_total,
    )
    done_urls, done_pids = collect_done_tracking(resume, scrape_state)
    todo = [
        u
        for u in urls
        if not url_already_scraped(u, done_urls, done_pids)
    ]
    n_skip = len(urls) - len(todo)
    print(
        f"[run] {len(todo)} URL(s) to visit  ({n_skip} skipped: "
        f"exact and/or product id already in log / pages / DB"
        f"{' / progress' if resume else ''})."
    )
    if not todo:
        print("[run] Nothing left to do.")
        return

    log_fh, log_w = open_log(resume)
    visited = skipped = errors = 0

    stop_sync = asyncio.Event()
    sync_task = None
    if scrape_state is not None:
        sync_task = asyncio.create_task(
            periodic_state_sync(scrape_state, stop_sync, state_sync_sec)
        )

    try:
        async with async_playwright() as p:
            if not wait_for_cdp(45.0, "[run] Waiting for Chrome DevTools (127.0.0.1:9222) …"):
                print(
                    f"\n[error] Nothing is listening on {CDP_HOST}:{CDP_PORT}.\n"
                    "  Chrome must be started with remote debugging. Run:\n"
                    "    python3 url_clicker.py --setup\n"
                    "  Wait until you see “CDP is listening”, then run with --cdp.\n"
                    "  (Opening Chrome from the Dock alone does not enable port 9222.)\n"
                )
                return
            try:
                browser = await p.chromium.connect_over_cdp(CDP, timeout=30_000)
            except Exception as exc:
                print(
                    f"\n[error] CDP connect failed ({CDP}): {exc}\n"
                    "  Run:   python3 url_clicker.py --setup   then   python3 url_clicker.py --cdp\n"
                    "  Or set CHROME_CDP_URL if Chrome uses another port.\n"
                )
                return

            print("[run] Connected to Chrome via CDP.")

            # use the first existing page, or open a new one
            ctx  = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            if pause_first_sec > 0:
                print(
                    f"[run] Pausing {pause_first_sec:.0f}s before first navigation "
                    "(inspect Chrome) …",
                    flush=True,
                )
                await asyncio.sleep(pause_first_sec)

            total = len(todo)
            for idx, url in enumerate(todo, 1):
                pid = _product_id(url)
                is_first = idx == 1
                nav_timeout = NAV_TIMEOUT_FIRST_MS if is_first else NAV_TIMEOUT_REST_MS
                prefix = f"[{idx}/{total}] {pid}"
                if is_first:
                    print(f"{prefix}  (first page — up to {NAV_TIMEOUT_FIRST_MS // 1000}s) …", flush=True)
                else:
                    print(f"{prefix}  ...", end="", flush=True)

                # navigate
                try:
                    resp = await page.goto(
                        url, wait_until="domcontentloaded", timeout=nav_timeout
                    )
                except Exception as exc:
                    print(f"  ERROR: {exc}")
                    log_w.writerow([pid, url, "nav_error", "", ""])
                    if scrape_state is not None:
                        scrape_state.record_result(
                            url,
                            pid,
                            "nav_error",
                            error_hint=str(exc)[:500],
                        )
                    log_fh.flush()
                    errors += 1
                    continue

                status_code = resp.status if resp else 0

                if status_code == 404:
                    print("  404 – skip")
                    log_w.writerow([pid, url, "404", "", ""])
                    if scrape_state is not None:
                        scrape_state.record_result(url, pid, "404")
                    log_fh.flush()
                    mark_done(url)
                    skipped += 1
                    continue

                if status_code in (401, 403, 429):
                    print(
                        f"  blocked — HTTP {status_code} (no usable PDP); "
                        f"not marking done; retry later"
                    )
                    log_w.writerow(
                        [pid, url, "blocked", f"http_{status_code}", ""]
                    )
                    if scrape_state is not None:
                        scrape_state.record_result(
                            url,
                            pid,
                            "blocked",
                            error_hint=f"HTTP {status_code}",
                        )
                    log_fh.flush()
                    errors += 1
                    continue

                await asyncio.sleep(
                    sample_pdp_dwell_sec(pdp_dwell_min, pdp_dwell_max)
                )

                # ── captcha check ───────────────────────────────────────────
                if await check_captcha(page):
                    if openai_api_key:
                        solved = await auto_solve_captcha(
                            page,
                            openai_api_key=openai_api_key,
                            manual_fallback_fn=wait_for_captcha_solve,
                        )
                    else:
                        await wait_for_captcha_solve(page)
                        solved = not await check_captcha(page)
                    # after solve: re-check; if still on captcha page navigate again
                    if not solved or await check_captcha(page):
                        try:
                            await page.goto(
                                url,
                                wait_until="domcontentloaded",
                                timeout=NAV_TIMEOUT_REST_MS,
                            )
                            await asyncio.sleep(1.0)
                        except Exception:
                            pass

                # ── save HTML (base snapshot before optional color PDP fetch) ─
                cdp_row_ok = False
                try:
                    html = await page.content()
                    if not shein_pdp_parseable_gb_raw_data(html):
                        print(
                            "  blocked — no parseable gbRawData (likely 403/WAF); "
                            "not marking done; retry later"
                        )
                        log_w.writerow(
                            [pid, url, "blocked", "no_gbRawData", ""]
                        )
                        if scrape_state is not None:
                            scrape_state.record_result(
                                url,
                                pid,
                                "blocked",
                                error_hint="no parseable window.gbRawData (likely 403)",
                            )
                        log_fh.flush()
                        errors += 1
                        continue

                    saved = save_html(pid, html)
                    av = write_availability_json(PAGES_DIR, pid, html)
                    print(f"  saved → pages/{pid}.html  ({len(html)//1024} KB)", end="")
                    if av is not None:
                        print(
                            f"  |  stock={av['stock']} sold_out={av['sold_out']}",
                            end="",
                        )
                    n_q = ingest_pdp_recommendation_pids(html, pid, url, scrape_state)
                    if n_q:
                        print(f"  |  +{n_q} id(s) queued for later (data-id / carousels)", end="")
                    if scrape_variants:
                        try:
                            vres = await scrape_variant_matrix(
                                page, pid, PAGES_DIR, pdp_url=url
                            )
                            if vres.waf_block:
                                print(
                                    "\n  [blocked] color-variant PDPs hit WAF (no gbRawData) — "
                                    "stopping CDP run; retry later",
                                    flush=True,
                                )
                                log_w.writerow(
                                    [pid, url, "blocked", "variant_waf", ""]
                                )
                                if scrape_state is not None:
                                    scrape_state.record_result(
                                        url,
                                        pid,
                                        "blocked",
                                        error_hint=(
                                            "variant navigation: no parseable gbRawData (WAF)"
                                        ),
                                    )
                                log_fh.flush()
                                errors += 1
                                return
                            if vres.extra_saved:
                                print(
                                    f"  |  color variants: {vres.extra_saved} extra PDP(s) → "
                                    f"pages/<product_key>.html"
                                )
                            else:
                                print(
                                    "  |  color variants: 0 (no extra color PDPs)"
                                )
                        except Exception as vexc:
                            print(f"  |  color variant scrape failed: {vexc}")
                    else:
                        print()
                    log_w.writerow([pid, url, "ok", saved, ""])
                    if scrape_state is not None:
                        scrape_state.record_result(
                            url, pid, "ok", saved_path=saved
                        )
                    visited += 1
                    cdp_row_ok = True
                except Exception as exc:
                    print(f"  save failed: {exc}")
                    log_w.writerow([pid, url, "save_error", "", ""])
                    if scrape_state is not None:
                        scrape_state.record_result(
                            url, pid, "save_error", error_hint=str(exc)[:500]
                        )
                    errors += 1

                log_fh.flush()
                if cdp_row_ok:
                    mark_done(url)
                dlo, dhi = (
                    DELAY_AFTER_SAVE_FIRST if is_first else DELAY_AFTER_SAVE_REST
                )
                await asyncio.sleep(random.uniform(dlo, dhi))

    finally:
        stop_sync.set()
        if sync_task is not None:
            sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sync_task
        if scrape_state is not None:
            scrape_state.wal_checkpoint()
        log_fh.close()

    print(f"\n[done] visited={visited}  skipped={skipped}  errors={errors}")
    print(f"       HTML pages → {PAGES_DIR}")
    if scrape_variants:
        print(f"       Color variant PDPs → {PAGES_DIR}/<product_key>.html")
    print(f"       Log        → {LOG_CSV}")
    if scrape_state is not None:
        print(f"       Scrape state → {scrape_state.summary()}")


async def run_proxy_mode(
    limit: int | None,
    resume: bool,
    scrape_variants: bool,
    proxy_file: Path,
    headless: bool,
    num_tabs: int = DEFAULT_PROXY_TABS,
    *,
    use_chrome: bool = False,
    strip_url_query: bool = False,
    pause_first_sec: float = 0.0,
    url_source: str = "pids",
    pids_path: Path = PIDS_FILE,
    urls_path: Path = URLS_FILE,
    scrape_state: "ScrapeState | None" = None,
    pdp_dwell_min: float = PDP_PAGE_DWELL_MIN_DEFAULT,
    pdp_dwell_max: float = PDP_PAGE_DWELL_MAX_DEFAULT,
    worker_index: int | None = None,
    worker_total: int | None = None,
    state_sync_sec: float = STATE_SYNC_SEC_DEFAULT,
    session_slots: int = DEFAULT_SESSION_SLOTS,
    browser_state: Path | None = None,
    openai_api_key: str | None = None,
):
    """
    Ephemeral (incognito) Playwright contexts, one Geonode proxy per session; the next
    session picks another available proxy (1-hour cooldown on the one that finished).
    With ``session_slots`` > 1, several of these run **in parallel** (separate
    context per slot; one shared work queue of URLs, claimed one at a time).
    Wall time per session is SESSION_WORK_SEC (login ends early). There is no PDP count
    cap—only time and login. At most MAX_TABS_PER_SESSION concurrent tabs (see
    ``num_tabs``). After single-tab warmup processes at least one URL, extra tabs open
    for parallel fetch.
    """
    proxies = load_proxy_lines(proxy_file)
    if not proxies:
        print(
            f"[error] No proxies in {proxy_file}. "
            "Add one Geonode sticky URL per line (http://user:pass@host:port)."
        )
        return

    state = ProxyState(BASE)
    urls = load_work_queue(
        limit,
        url_source=url_source,
        pids_path=pids_path,
        urls_path=urls_path,
        scrape_state=scrape_state,
        worker_index=worker_index,
        worker_total=worker_total,
    )
    done_urls, done_pids = collect_done_tracking(resume, scrape_state)
    todo = [
        u
        for u in urls
        if not url_already_scraped(u, done_urls, done_pids)
    ]
    n_skip = len(urls) - len(todo)
    print(
        f"[run] {len(todo)} URL(s) to visit  ({n_skip} skipped: "
        f"exact and/or product id already in log / pages / DB"
        f"{' / progress' if resume else ''})."
    )
    if not todo:
        print("[run] Nothing left to do.")
        return

    _uniq_todo = list(dict.fromkeys(todo))
    if len(_uniq_todo) != len(todo):
        print(
            f"[run] de-duplicated queue: {len(todo)} → {len(_uniq_todo)} unique URL(s).",
            flush=True,
        )
    todo = _uniq_todo

    log_fh, log_w = open_log(resume)
    visited = skipped = errors = 0

    stop_sync = asyncio.Event()
    sync_task = None
    if scrape_state is not None:
        sync_task = asyncio.create_task(
            periodic_state_sync(scrape_state, stop_sync, state_sync_sec)
        )

    try:
        async with async_playwright() as p:
            launch_kw: dict = {
                "headless": headless,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            if use_chrome:
                launch_kw["channel"] = "chrome"
            browser = await p.chromium.launch(**launch_kw)
            try:
                stats_lock = asyncio.Lock()
                proxy_pick_lock = asyncio.Lock()
                work_deque_lock = asyncio.Lock()
                work_deque: deque[str] = deque(todo)
                in_flight_lock = asyncio.Lock()
                in_flight_urls = 0
                n_slots = max(1, min(session_slots, 32))
                slots_first_pause: set[int] = set()

                print(
                    f"[proxy] {n_slots} parallel session slot(s)  |  shared work queue: "
                    f"{len(todo)} unique URL(s) (one claim per product at a time across slots)",
                    flush=True,
                )
                if browser_state is not None:
                    if browser_state.exists():
                        print(
                            f"[proxy] Reusing saved session cookies/localStorage: {browser_state}",
                            flush=True,
                        )
                    else:
                        print(
                            f"[proxy] No file at {browser_state} — starting clean. "
                            "Prime once with:  python3 url_clicker.py --prime-browser-state",
                            flush=True,
                        )

                async def _claim_work_url() -> str | None:
                    nonlocal in_flight_urls
                    async with work_deque_lock:
                        if not work_deque:
                            return None
                        u = work_deque.popleft()
                    async with in_flight_lock:
                        in_flight_urls += 1
                    return u

                async def _requeue_work_url_front(url: str) -> None:
                    async with work_deque_lock:
                        work_deque.appendleft(url)

                async def _end_work_in_flight() -> None:
                    nonlocal in_flight_urls
                    async with in_flight_lock:
                        in_flight_urls -= 1

                async def _slot_work(slot_id: int) -> None:
                    nonlocal visited, errors, skipped
                    while True:
                        async with work_deque_lock:
                            dq0 = not work_deque
                        async with in_flight_lock:
                            if0 = in_flight_urls
                        if dq0 and if0 == 0:
                            return
                        async with proxy_pick_lock:
                            proxy_url = pick_next_proxy(proxies, state)
                        while proxy_url is None:
                            wsec = wait_seconds_for_next_proxy(state, proxies)
                            if wsec <= 0:
                                await asyncio.sleep(0.5)
                            else:
                                print(
                                    f"[proxy] All proxies in cooldown; waiting {wsec:.0f}s …",
                                    flush=True,
                                )
                                await asyncio.sleep(min(wsec + 0.5, 600))
                            async with proxy_pick_lock:
                                proxy_url = pick_next_proxy(proxies, state)
    
                        proxy_label = mask_proxy_url(proxy_url)
    
                        try:
                            pw_proxy = parse_playwright_proxy(proxy_url)
                        except ValueError as exc:
                            print(f"[error] {exc}")
                            errors += 1
                            continue
    
                        ctx_params: dict = {
                            "proxy": pw_proxy,
                            "ignore_https_errors": True,
                            "user_agent": CHROME_USER_AGENT,
                            "locale": "en-US",
                            "timezone_id": "America/New_York",
                            "viewport": {"width": 1365, "height": 900},
                            "extra_http_headers": {
                                "Accept-Language": "en-US,en;q=0.9",
                            },
                        }
                        if (
                            browser_state is not None
                            and browser_state.exists()
                        ):
                            ctx_params["storage_state"] = str(browser_state)
                        context = await browser.new_context(**ctx_params)
                        n_tabs = max(1, min(num_tabs, MAX_TABS_PER_SESSION))

                        session_start = time.monotonic()
                        cooldown_marked = False
                        login_abort = False
                        lock = stats_lock
    
                        def _drain_queue_to_todo() -> None:
                            """Unstarted URLs live in the shared work_deque; nothing to drain from here."""
                            return

                        async def _session_abort(
                            requeue_url: str, mark_reason: str, banner: str
                        ) -> None:
                            """End this proxy context: cooldown, put URL back on the shared work deque."""
                            nonlocal login_abort, cooldown_marked
                            async with lock:
                                if not login_abort:
                                    login_abort = True
                                    cooldown_marked = True
                                    state.mark_cooldown(proxy_url, mark_reason)
                            await _requeue_work_url_front(requeue_url)
                            print(banner, flush=True)
    
                        async def process_url_line(
                            page, url: str, tab_label: str, is_first_nav: bool
                        ) -> str:
                            """
                            Navigate, captcha, save. Returns:
                            ok | nav_error | 404 | login | blocked | save_error
                            """
                            nonlocal visited, errors, skipped, cooldown_marked, login_abort
                            pid = _product_id(url)
                            nav_timeout = (
                                NAV_TIMEOUT_FIRST_MS
                                if is_first_nav
                                else NAV_TIMEOUT_REST_MS
                            )
                            print(
                                f"  [tab {tab_label}] {pid}  ...",
                                end="",
                                flush=True,
                            )
    
                            nav_url = (
                                url.split("?", 1)[0]
                                if strip_url_query and "?" in url
                                else url
                            )
    
                            try:
                                resp = await page.goto(
                                    nav_url,
                                    wait_until="domcontentloaded",
                                    timeout=nav_timeout,
                                )
                            except Exception as exc:
                                hint = _nav_error_hint(exc)
                                print(f"  ERROR: {exc}")
                                if hint:
                                    print(hint)
                                async with lock:
                                    log_w.writerow(
                                        [pid, url, "nav_error", "", proxy_label]
                                    )
                                    if scrape_state is not None:
                                        scrape_state.record_result(
                                            url,
                                            pid,
                                            "nav_error",
                                            error_hint=str(exc)[:500],
                                            proxy_label=proxy_label,
                                        )
                                    log_fh.flush()
                                    errors += 1
                                return "nav_error"
    
                            status_code = resp.status if resp else 0
    
                            if status_code == 404:
                                print("  404 – skip")
                                async with lock:
                                    log_w.writerow(
                                        [pid, url, "404", "", proxy_label]
                                    )
                                    if scrape_state is not None:
                                        scrape_state.record_result(
                                            url, pid, "404", proxy_label=proxy_label
                                        )
                                    log_fh.flush()
                                    mark_done(url)
                                    skipped += 1
                                return "404"
    
                            if status_code in (401, 403, 429):
                                async with lock:
                                    log_w.writerow(
                                        [
                                            pid,
                                            url,
                                            "blocked",
                                            f"http_{status_code}",
                                            proxy_label,
                                        ]
                                    )
                                    if scrape_state is not None:
                                        scrape_state.record_result(
                                            url,
                                            pid,
                                            "blocked",
                                            error_hint=f"HTTP {status_code}",
                                            proxy_label=proxy_label,
                                        )
                                    log_fh.flush()
                                await _session_abort(
                                    url,
                                    f"http_{status_code}",
                                    f"\n  [blocked] HTTP {status_code} on PDP — "
                                    f"stopping session; 1h cooldown for {proxy_label}",
                                )
                                return "blocked"
    
                            await asyncio.sleep(
                                sample_pdp_dwell_sec(
                                    pdp_dwell_min, pdp_dwell_max
                                )
                            )
    
                            if await check_login_page(page):
                                await _session_abort(
                                    url,
                                    "login",
                                    f"\n  [login] Stopping session; 1h cooldown for "
                                    f"{proxy_label}",
                                )
                                return "login"
    
                            if await check_captcha(page):
                                if openai_api_key:
                                    async def _manual_fb(p):
                                        await wait_for_captcha_solve(p, tab_label=tab_label)
                                    solved = await auto_solve_captcha(
                                        page,
                                        openai_api_key=openai_api_key,
                                        manual_fallback_fn=_manual_fb,
                                    )
                                else:
                                    await wait_for_captcha_solve(page, tab_label=tab_label)
                                    solved = not await check_captcha(page)
                                if not solved or await check_captcha(page):
                                    try:
                                        await page.goto(
                                            nav_url,
                                            wait_until="domcontentloaded",
                                            timeout=NAV_TIMEOUT_REST_MS,
                                        )
                                        await asyncio.sleep(1.0)
                                    except Exception:
                                        pass
    
                            if await check_login_page(page):
                                await _session_abort(
                                    url,
                                    "login",
                                    f"\n  [login] Stopping session; 1h cooldown for "
                                    f"{proxy_label}",
                                )
                                return "login"
    
                            html = await page.content()
                            if not shein_pdp_parseable_gb_raw_data(html):
                                async with lock:
                                    log_w.writerow(
                                        [
                                            pid,
                                            url,
                                            "blocked",
                                            "no_gbRawData",
                                            proxy_label,
                                        ]
                                    )
                                    if scrape_state is not None:
                                        scrape_state.record_result(
                                            url,
                                            pid,
                                            "blocked",
                                            error_hint=(
                                                "no parseable window.gbRawData (likely 403/WAF page)"
                                            ),
                                            proxy_label=proxy_label,
                                        )
                                    log_fh.flush()
                                await _session_abort(
                                    url,
                                    "no_gb_raw_data",
                                    f"\n  [blocked] no parseable gbRawData (likely 403) — "
                                    f"stopping session; 1h cooldown for {proxy_label}",
                                )
                                return "blocked"
    
                            save_ok = False
                            try:
                                saved = save_html(pid, html)
                                av = write_availability_json(PAGES_DIR, pid, html)
                                print(
                                    f"  saved → pages/{pid}.html  ({len(html)//1024} KB)",
                                    end="",
                                )
                                if av is not None:
                                    print(
                                        f"  |  stock={av['stock']} sold_out={av['sold_out']}",
                                        end="",
                                    )
                                n_q = ingest_pdp_recommendation_pids(
                                    html, pid, url, scrape_state
                                )
                                if n_q:
                                    print(
                                        f"  |  +{n_q} id(s) queued (data-id / carousels)",
                                        end="",
                                    )
                                if scrape_variants:
                                    try:
                                        vres = await scrape_variant_matrix(
                                            page,
                                            pid,
                                            PAGES_DIR,
                                            pdp_url=url,
                                        )
                                        if vres.waf_block:
                                            await _session_abort(
                                                url,
                                                "no_gb_raw_data",
                                                f"\n  [blocked] color-variant PDPs hit WAF "
                                                f"(no gbRawData) — stopping session; 1h cooldown "
                                                f"for {proxy_label}",
                                            )
                                            return "blocked"
                                        if vres.extra_saved:
                                            print(
                                                f"  |  color variants: {vres.extra_saved} extra "
                                                f"PDP(s) → pages/<product_key>.html"
                                            )
                                        else:
                                            print(
                                                "  |  color variants: 0 (no extra color PDPs)"
                                            )
                                    except Exception as vexc:
                                        print(
                                            f"  |  color variant scrape failed: {vexc}"
                                        )
                                else:
                                    print()
                                async with lock:
                                    log_w.writerow(
                                        [pid, url, "ok", saved, proxy_label]
                                    )
                                    if scrape_state is not None:
                                        scrape_state.record_result(
                                            url,
                                            pid,
                                            "ok",
                                            saved_path=saved,
                                            proxy_label=proxy_label,
                                        )
                                    visited += 1
                                save_ok = True
                            except Exception as exc:
                                print(f"  save failed: {exc}")
                                async with lock:
                                    log_w.writerow(
                                        [pid, url, "save_error", "", proxy_label]
                                    )
                                    if scrape_state is not None:
                                        scrape_state.record_result(
                                            url,
                                            pid,
                                            "save_error",
                                            error_hint=str(exc)[:500],
                                            proxy_label=proxy_label,
                                        )
                                    errors += 1
    
                            async with lock:
                                log_fh.flush()
                                if save_ok:
                                    mark_done(url)
    
                            dlo, dhi = (
                                DELAY_AFTER_SAVE_FIRST
                                if is_first_nav
                                else DELAY_AFTER_SAVE_REST
                            )
                            await asyncio.sleep(random.uniform(dlo, dhi))
    
                            return "ok" if save_ok else "save_error"
    
                        page0 = await context.new_page()
                        pages: list = [page0]
                        warmup_nav_count = 0
    
                        if (
                            pause_first_sec > 0
                            and slot_id not in slots_first_pause
                        ):
                            slots_first_pause.add(slot_id)
                            print(
                                f"[proxy] [slot {slot_id + 1}] Pausing {pause_first_sec:.0f}s before first "
                                f"navigation (1 tab — solve captcha / inspect proxy) …",
                                flush=True,
                            )
                            await asyncio.sleep(pause_first_sec)
    
                        warmup_ok = False
                        while not warmup_ok and not login_abort:
                            if time.monotonic() - session_start >= SESSION_WORK_SEC:
                                break
                            url = await _claim_work_url()
                            if url is None:
                                async with work_deque_lock:
                                    dq0 = not work_deque
                                async with in_flight_lock:
                                    if0 = in_flight_urls
                                if dq0 and if0 == 0:
                                    break
                                await asyncio.sleep(0.2)
                                continue
                            try:
                                is_first_warmup = warmup_nav_count == 0
                                r = await process_url_line(
                                    page0, url, "1", is_first_warmup
                                )
                            finally:
                                await _end_work_in_flight()
                            warmup_nav_count += 1
                            if r in ("login", "blocked"):
                                break
                            if r == "ok":
                                warmup_ok = True
                                break
    
                        if n_tabs > 1 and not login_abort and warmup_nav_count > 0:
                            print(
                                f"[proxy] Opening {n_tabs - 1} more tab(s) for parallel fetch "
                                f"(warmup navigations={warmup_nav_count}, saved_ok={warmup_ok}) …",
                                flush=True,
                            )
                            for _ in range(n_tabs - 1):
                                pages.append(await context.new_page())
                        elif n_tabs > 1 and warmup_nav_count == 0:
                            print(
                                "[proxy] No URL reached in warmup — using 1 tab only.",
                                flush=True,
                            )
    
                        async def tab_worker(tab_id: int, page) -> None:
                            nonlocal login_abort
                            tab_label = str(tab_id + 1)
                            first_in_tab = True
                            if tab_id == 0 and warmup_nav_count > 0:
                                first_in_tab = False
    
                            while True:
                                if login_abort:
                                    break
                                if time.monotonic() - session_start >= SESSION_WORK_SEC:
                                    break
                                url = await _claim_work_url()
                                if url is None:
                                    if login_abort:
                                        break
                                    async with work_deque_lock:
                                        dq0 = not work_deque
                                    async with in_flight_lock:
                                        if0 = in_flight_urls
                                    if dq0 and if0 == 0:
                                        break
                                    await asyncio.sleep(0.2)
                                    continue
                                try:
                                    is_first_nav = first_in_tab
                                    if first_in_tab:
                                        first_in_tab = False
                                    r = await process_url_line(
                                        page, url, tab_label, is_first_nav
                                    )
                                finally:
                                    await _end_work_in_flight()
                                if r in ("login", "blocked"):
                                    break
    
                        try:
                            n_effective = len(pages)
                            async with work_deque_lock:
                                n_pending = len(work_deque)
                            async with in_flight_lock:
                                infl = in_flight_urls
                            print(
                                f"[proxy] [slot {slot_id + 1}] {proxy_label}  |  "
                                f"{n_effective} parallel tab(s)  |  "
                                f"{n_pending} pending, {infl} in flight  |  "
                                f"{SESSION_WORK_SEC:.0f}s wall time (or login / block ends session)",
                                flush=True,
                            )
                            await asyncio.gather(
                                *[
                                    tab_worker(i, pages[i])
                                    for i in range(n_effective)
                                ],
                                return_exceptions=True,
                            )
                        finally:
                            if not login_abort:
                                _drain_queue_to_todo()
                            await context.close()
                            if not cooldown_marked:
                                state.mark_cooldown(proxy_url, "session_end")
    
                await asyncio.gather(
                    *(_slot_work(i) for i in range(n_slots))
                )
            finally:
                await browser.close()

    finally:
        stop_sync.set()
        if sync_task is not None:
            sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sync_task
        if scrape_state is not None:
            scrape_state.wal_checkpoint()
        log_fh.close()

    print(f"\n[done] visited={visited}  skipped={skipped}  errors={errors}")
    print(f"       HTML pages → {PAGES_DIR}")
    print(f"       Proxy state → {BASE / 'proxy_state.json'}")
    if scrape_variants:
        print(f"       Color variant PDPs → {PAGES_DIR}/<product_key>.html")
    print(f"       Log        → {LOG_CSV}")
    if scrape_state is not None:
        print(f"       Scrape state → {scrape_state.summary()}")


# ───────────────────────────────────────────────────────────────────────────
# entry
# ───────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="SHEIN URL clicker + page saver")
    ap.add_argument("--setup",  action="store_true",
                    help="Relaunch Chrome with CDP on 9222 (Dileep K profile by default)")
    ap.add_argument(
        "--profile",
        default=DEFAULT_PROFILE_DIR,
        metavar="DIR",
        help=(
            'Chrome profile folder name (default: %(default)s = "Dileep K" / second tile; '
            'use "Profile 1" for anakin.company)'
        ),
    )
    ap.add_argument("--limit",  type=int, default=None,
                    help="Max unique queue entries to process")
    ap.add_argument(
        "--url-source",
        choices=("pids", "urls"),
        default="pids",
        help=(
            "pids: build US PDP URLs from --pids-file (same path template as shein_url_builder.py). "
            "urls: read one URL per line from --urls-file (legacy list)."
        ),
    )
    ap.add_argument(
        "--pids-file",
        type=Path,
        default=PIDS_FILE,
        metavar="PATH",
        help="Product ids: JSON array or one id per line (default: ./pids.txt) when --url-source=pids",
    )
    ap.add_argument(
        "--urls-file",
        type=Path,
        default=URLS_FILE,
        metavar="PATH",
        help="Line-based URL list (default: ./urls) when --url-source=urls",
    )
    ap.add_argument("--resume", action="store_true",
                    help="Skip URLs already in progress.txt")
    ap.add_argument(
        "--prune-scraped",
        action="store_true",
        help="Rewrite urls: drop lines already in pages/, progress.txt, log.csv (ok). Backs up to urls.bak",
    )
    ap.add_argument(
        "--no-variants",
        action="store_true",
        help=(
            "Do not open other color PDPs. By default, other colors are saved as pages/<product_key>.html "
            "(allColorDetailImages keys); DOM fallback: pages/<pid>_color<ci>.html (no size clicks)."
        ),
    )
    ap.add_argument(
        "--cdp",
        action="store_true",
        help="Attach to Chrome on port 9222 (use --setup first) instead of proxy + incognito",
    )
    ap.add_argument(
        "--proxy-file",
        type=Path,
        default=PROXY_FILE_DEFAULT,
        metavar="PATH",
        help="Geonode proxy list: one http://user:pass@host:port per line (default: ./geonode_proxies.txt)",
    )
    ap.add_argument(
        "--openai-key",
        type=str,
        default=None,
        metavar="KEY",
        help=(
            "OpenAI API key for image-sequence captcha auto-solve (GPT-4o vision). "
            "Alternatively set the OPENAI_API_KEY environment variable. "
            "If absent, falls back to manual Enter-in-terminal when image-seq captcha fires."
        ),
    )
    ap.add_argument(
        "--prime-browser-state",
        nargs="?",
        const=DEFAULT_BROWSER_STATE_FILE,
        default=None,
        type=Path,
        metavar="FILE",
        help=(
            "Headed browser: open us.shein.com, you solve any captcha, save Playwright state "
            f"to FILE (default: {DEFAULT_BROWSER_STATE_FILE.name}) and exit. "
            "Then run with --browser-state. Uses first proxy in --proxy-file when present."
        ),
    )
    ap.add_argument(
        "--browser-state",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            "Load saved storage state from --prime-browser-state so the first navigations "
            "reuse cookies/localStorage. New IP (rotating proxy) may still trigger a challenge."
        ),
    )
    ap.add_argument(
        "--headless",
        action="store_true",
        help="Chromium headless in proxy mode (no visible window; captcha harder to solve)",
    )
    ap.add_argument(
        "--tabs",
        type=int,
        default=DEFAULT_PROXY_TABS,
        metavar="N",
        help=(
            "Concurrent browser tabs per proxy session (default 2, "
            f"max {MAX_TABS_PER_SESSION}; extra tabs open after warmup once any URL is processed)"
        ),
    )
    ap.add_argument(
        "--session-slots",
        type=int,
        default=DEFAULT_SESSION_SLOTS,
        metavar="N",
        help=(
            "In proxy mode, how many independent sessions run at once (shared work queue; "
            f"default {DEFAULT_SESSION_SLOTS}; each session = one context + one proxy line)"
        ),
    )
    ap.add_argument(
        "--chrome",
        action="store_true",
        help=(
            "Use installed Google Chrome instead of bundled Chromium (proxy mode; "
            "better fingerprint; requires Chrome on PATH / default macOS location)"
        ),
    )
    ap.add_argument(
        "--strip-url-query",
        action="store_true",
        help="Navigate to URL without ?query string (proxy mode; can reduce blocked long URLs)",
    )
    ap.add_argument(
        "--pause-first",
        type=float,
        default=0.0,
        metavar="SEC",
        help=(
            "Wait SEC seconds after browser opens, before first navigation(s). "
            "Use e.g. 60 to watch the first run. Omit or 0 for no pause."
        ),
    )
    ap.add_argument(
        "--scrape-state-db",
        type=Path,
        default=SCRAPE_STATE_DB,
        metavar="PATH",
        help="SQLite (WAL) for per-URL outcomes + discovered pids; safe for parallel sessions",
    )
    ap.add_argument(
        "--no-scrape-state",
        action="store_true",
        help="Disable SQLite, data-id discovery queue, and periodic checkpoint",
    )
    ap.add_argument(
        "--pdp-dwell-sec",
        type=float,
        default=None,
        metavar="SEC",
        help=(
            "Fixed dwell after domcontentloaded (seconds). If omitted, use --pdp-dwell-min/max "
            f"(default random {PDP_PAGE_DWELL_MIN_DEFAULT}–{PDP_PAGE_DWELL_MAX_DEFAULT}s per page)."
        ),
    )
    ap.add_argument(
        "--pdp-dwell-min",
        type=float,
        default=PDP_PAGE_DWELL_MIN_DEFAULT,
        metavar="SEC",
        help=f"Random dwell lower bound when --pdp-dwell-sec is omitted (default {PDP_PAGE_DWELL_MIN_DEFAULT}).",
    )
    ap.add_argument(
        "--pdp-dwell-max",
        type=float,
        default=PDP_PAGE_DWELL_MAX_DEFAULT,
        metavar="SEC",
        help=f"Random dwell upper bound when --pdp-dwell-sec is omitted (default {PDP_PAGE_DWELL_MAX_DEFAULT}).",
    )
    ap.add_argument(
        "--worker",
        type=int,
        default=None,
        metavar="I",
        help="Parallel sharding: keep only queue rows with index %% N == I (use with --workers).",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help="Total parallel processes for sharding (e.g. 3 workers: run with --worker 0, 1, 2).",
    )
    ap.add_argument(
        "--state-sync-sec",
        type=float,
        default=STATE_SYNC_SEC_DEFAULT,
        metavar="SEC",
        help="WAL checkpoint + state line every SEC (default 7 min)",
    )
    args = ap.parse_args()

    if args.pdp_dwell_sec is not None:
        pdp_min = pdp_max = float(args.pdp_dwell_sec)
    else:
        pdp_min, pdp_max = float(args.pdp_dwell_min), float(args.pdp_dwell_max)
        if pdp_max < pdp_min:
            pdp_min, pdp_max = pdp_max, pdp_min

    scrape_st: ScrapeState | None = None
    if not args.no_scrape_state:
        scrape_st = ScrapeState(args.scrape_state_db)

    if args.prune_scraped:
        prune_scraped_from_urls_file()
        return

    if args.prime_browser_state is not None:
        asyncio.run(
            run_prime_browser_state(
                args.prime_browser_state,
                proxy_file=args.proxy_file,
                use_chrome=args.chrome,
            )
        )
        return

    if args.setup:
        setup_chrome(args.profile)

    if args.cdp:
        asyncio.run(
            run_cdp(
                args.limit,
                args.resume,
                scrape_variants=not args.no_variants,
                pause_first_sec=args.pause_first,
                url_source=args.url_source,
                pids_path=args.pids_file,
                urls_path=args.urls_file,
                scrape_state=scrape_st,
                pdp_dwell_min=pdp_min,
                pdp_dwell_max=pdp_max,
                worker_index=args.worker,
                worker_total=args.workers,
                state_sync_sec=args.state_sync_sec,
                openai_api_key=args.openai_key or os.environ.get("OPENAI_API_KEY"),
            )
        )
    else:
        asyncio.run(
            run_proxy_mode(
                args.limit,
                args.resume,
                scrape_variants=not args.no_variants,
                proxy_file=args.proxy_file,
                headless=args.headless,
                num_tabs=max(1, min(args.tabs, MAX_TABS_PER_SESSION)),
                use_chrome=args.chrome,
                strip_url_query=args.strip_url_query,
                pause_first_sec=args.pause_first,
                url_source=args.url_source,
                pids_path=args.pids_file,
                urls_path=args.urls_file,
                scrape_state=scrape_st,
                pdp_dwell_min=pdp_min,
                pdp_dwell_max=pdp_max,
                worker_index=args.worker,
                worker_total=args.workers,
                state_sync_sec=args.state_sync_sec,
                session_slots=max(1, min(args.session_slots, 32)),
                browser_state=args.browser_state,
                openai_api_key=args.openai_key or os.environ.get("OPENAI_API_KEY"),
            )
        )


if __name__ == "__main__":
    main()
