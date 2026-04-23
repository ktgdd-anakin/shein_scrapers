"""
SHEIN category (PLP) → save each listing page as HTML, then collect product ids (-p-…).

Uses Playwright like url_clicker: real browser + desktop UA. Category grids are
Vue/CSR-heavy; we use ``domcontentloaded`` + a short settle (not a raw HTTP fetch),
so the saved file is the document after initial parse — for a fuller grid, increase
``--settle-sec`` or use ``--wait load``.

Example:
  python3 PLP_shein/plp_get_pids.py --url "https://us.shein.com/Food-Beverages-c-13086.html"
  # Opens a visible Chromium window by default. Use --headless for no window.
  python3 PLP_shein/plp_get_pids.py --wait-for-plp-sec 0 --max-pages 5
  python3 PLP_shein/plp_get_pids.py --chrome   # use Google Chrome instead of bundled Chromium
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from playwright.async_api import async_playwright

# Default category from your constants
category_ids = ["13086"]
category_names = ["Food-Beverages"]

BASE = Path(__file__).resolve().parent
PLP_HTML_DIR = BASE / "plp_html"

# Match url_clicker / real Chrome
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# First SHEIN page is slow; match url_clicker.NAV_TIMEOUT_FIRST_MS
NAV_TIMEOUT_FIRST_MS = 120_000
# At least this long in the tab before the terminal asks for Enter (captcha path).
MIN_CAPTCHA_SOLVE_SEC_DEFAULT = 120.0

# Identical to url_clicker.CAPTCHA_SIGNALS
CAPTCHA_SIGNALS = [
    "verify you are human",
    "i am human",
    "please select the following",
    "verify-wrap",
    "captcha-verify",
    "risk/challenge",
]


def _notify(msg: str) -> None:
    """macOS system notification + terminal bell (same as url_clicker)."""
    print(f"\a\n{'!'*60}\n  {msg}\n{'!'*60}\n")
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{msg}" with title "plp_get_pids" sound name "Basso"',
            ],
            capture_output=True,
        )
    except Exception:
        pass


async def check_captcha(page) -> bool:
    """
    Detect challenge / captcha without ``page.content()`` (that call can take **minutes**
    on large SHEIN pages and looks like a hang after you press Enter).
    Same signals as url_clicker; scan URL + body text + a bounded HTML slice in-page.
    """
    if "risk/challenge" in page.url:
        return True
    try:
        return await page.evaluate(
            """(sigs) => {
              const body = document.body;
              if (!body) return false;
              const t = (body.innerText || "").toLowerCase();
              for (const s of sigs) { if (t.includes(s)) return true; }
              const raw = (body.innerHTML || "").toLowerCase();
              const h = raw.length > 1800000 ? raw.slice(0, 1800000) : raw;
              for (const s of sigs) { if (h.includes(s)) return true; }
              return false;
            }""",
            CAPTCHA_SIGNALS,
        )
    except Exception:
        return False


async def wait_for_captcha_solve(
    page,
    tab_label: str | None = None,
    *,
    min_solve_sec: float = 0.0,
) -> None:
    """
    Same flow as url_clicker.wait_for_captcha_solve, plus an optional
    ``min_solve_sec`` wait *before* asking for Enter (gives you time in the
    browser — default 2 minutes when used from ``resolve_captcha_if_needed``).
    """
    where = f" (tab {tab_label})" if tab_label else ""
    _notify(
        f"CAPTCHA detected{where}!  Solve it in that Chromium tab, then press Enter here."
    )
    if min_solve_sec > 0.0:
        print(
            f"  [captcha] Waiting {min_solve_sec:.0f}s — solve in the browser; "
            f"then press Enter here to continue.",
            flush=True,
        )
        await asyncio.sleep(min_solve_sec)
    prompt = (
        f"  >> Tab {tab_label}: Press Enter after solving captcha: "
        if tab_label
        else "  >> Press Enter after solving captcha: "
    )
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, input, prompt)
    print(
        "[captcha] Enter received — pausing 1.5s for the page to settle, "
        "then the crawl continues (watch for [plp] lines).",
        flush=True,
    )
    await asyncio.sleep(1.5)


async def resolve_captcha_if_needed(
    page,
    *,
    plp_reload_url: str,
    min_solve_sec: float,
) -> None:
    """
    If a captcha is present: notify, wait min_solve_sec, Enter, then reload
    the listing URL if still on captcha (url_clicker pattern after solve).
    """
    if not await check_captcha(page):
        return
    await wait_for_captcha_solve(page, min_solve_sec=min_solve_sec)
    print(
        "[captcha] Re-checking after Enter (still fast; should not hang)…",
        flush=True,
    )
    if await check_captcha(page):
        print(
            "[captcha] Page still looks like a challenge — reloading the listing (up to "
            f"{NAV_TIMEOUT_FIRST_MS // 1000}s)…",
            flush=True,
        )
        try:
            await page.goto(
                plp_reload_url,
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT_FIRST_MS,
            )
            await asyncio.sleep(1.0)
            print("[captcha] Reload finished.", flush=True)
        except Exception as e:
            print(f"[captcha] Reload failed: {e!r} — continuing anyway.", flush=True)
    else:
        print("[captcha] Challenge cleared — continuing to PLP capture.", flush=True)


# Product links on PLP: …-p-12345678.html
PID_PATTERN = re.compile(
    r"(?:https?://[^\"'\s<]+?)?-p-(\d{6,16})\.html",
    re.IGNORECASE,
)


async def get_page_source_html(page) -> str:
    """
    “Page source” for automation: the **current live DOM** (doctype + ``<html>``),
    the same kind of string as DevTools → Elements → ``<html>`` → Copy → outerHTML.
    Browsers’ **File → Save Page** and **View Page Source** are unreliable for
    heavy SPAs; ``page.content()`` is similar to this, but the in-DOM
    ``outerHTML`` path matches what most people expect for “get the real HTML”.

    This uses the main frame only (same as our pid extraction target).
    """
    return await page.evaluate(
        """() => {
        let p = "";
        if (document.doctype) {
            try {
                p = new XMLSerializer().serializeToString(document.doctype) + "\\n";
            } catch (e) {
                p = "<!DOCTYPE html>\\n";
            }
        } else {
            p = "<!DOCTYPE html>\\n";
        }
        if (!document.documentElement) {
            return p + "<html><body></body></html>";
        }
        return p + document.documentElement.outerHTML;
    }""",
    )

# At least one product tile link; captcha / interstitial usually has none.
PLP_PRODUCT_LINK = "a[href*='-p-']"


async def _wait_for_selector_with_heartbeat(
    page,
    selector: str,
    timeout_ms: int,
    label: str,
) -> None:
    """
    Playwright can sit silently on long ``wait_for_selector`` calls. Emit a
    line every 12s while waiting (infinite or long timeouts).
    """
    print(
        f"[wait] {label} (timeout={'none' if timeout_ms == 0 else f'{timeout_ms} ms'})…",
        flush=True,
    )

    async def _beat() -> None:
        step = 12.0
        n = 0
        try:
            while True:
                await asyncio.sleep(step)
                n += 1
                print(
                    f"[wait] {label} — {n * step:.0f}s, still waiting for selector…",
                    flush=True,
                )
        except asyncio.CancelledError:
            raise

    if 0 < timeout_ms < 30_000:
        await page.wait_for_selector(selector, timeout=timeout_ms)
        return

    b = asyncio.create_task(_beat())
    try:
        await page.wait_for_selector(selector, timeout=timeout_ms)
    finally:
        b.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await b


# SHEIN / sui pagination — next is often a <button> in .sui-pagination
NEXT_SELECTORS: tuple[str, ...] = (
    "button.sui-pagination__btn-next:not([disabled])",
    "button.sui-pagination__btn-next:not(.sui-pagination--disabled)",
    ".sui-pagination button.sui-pagination__btn-next:not([disabled])",
    "a.sui-pagination__btn-next[href]",
    "a[rel='next']",
    "div.product-list__next-holder button",
)


def get_categories_url(category_name: str, category_id: str, page: int = 1) -> str:
    base = f"https://us.shein.com/{category_name}-c-{category_id}.html"
    if page <= 1:
        return base
    return f"{base}?page={page}"


def extract_pids_from_html(html: str) -> set[str]:
    return {m.group(1) for m in PID_PATTERN.finditer(html)}


def _next_url_with_page_param(url: str, page_num: int) -> str:
    """If URL already has ?page= or we add it, return URL for 1-based page index."""
    p = urlparse(url)
    q = parse_qs(p.query, keep_blank_values=True)
    flat: dict[str, str] = {k: v[-1] for k, v in q.items() if v}
    flat["page"] = str(page_num)
    new_query = urlencode(flat)
    return urlunparse(
        (p.scheme, p.netloc, p.path, p.params, new_query, p.fragment)
    )


async def _try_click_next(page) -> bool:
    for sel in NEXT_SELECTORS:
        loc = page.locator(sel).first
        try:
            n = await loc.count()
        except Exception:
            continue
        if n == 0:
            continue
        try:
            if not await loc.is_visible():
                continue
        except Exception:
            continue
        try:
            dis = await loc.get_attribute("disabled")
            if dis in ("", "true", "disabled"):
                continue
        except Exception:
            pass
        try:
            await loc.click(timeout=20_000)
            return True
        except Exception:
            continue
    return False


async def _crawl_plp(
    start_url: str,
    out_dir: Path,
    *,
    max_pages: int,
    headless: bool,
    use_chrome: bool,
    settle_first_sec: float,
    settle_step_sec: float,
    use_page_query_fallback: bool,
    wait_for_plp_timeout_ms: int | None,
    captcha_min_solve_sec: float,
    skip_captcha_flow: bool,
    use_playwright_page_content: bool,
    save_initial_network_html: bool,
) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    all_pids: set[str] = set()
    log_lines: list[str] = []
    t0 = time.monotonic()

    async with async_playwright() as p:
        launch_kw: dict = {
            "headless": headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if use_chrome:
            launch_kw["channel"] = "chrome"
        browser = await p.chromium.launch(**launch_kw)
        try:
            if not headless:
                if use_chrome:
                    print(
                        "[browser] Opening Google Chrome (visible). "
                        "Check the Dock if the window is behind other apps.",
                        flush=True,
                    )
                else:
                    print(
                        "[browser] Opening Playwright Chromium (visible, not Chrome.app). "
                        "Add --chrome to use Google Chrome. "
                        "If you do not see a window, check the Dock and other Spaces.",
                        flush=True,
                    )
            context = await browser.new_context(
                user_agent=CHROME_USER_AGENT,
                locale="en-US",
                timezone_id="America/New_York",
                viewport={"width": 1400, "height": 900},
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            page = await context.new_page()

            current_url = start_url
            print(
                f"[plp] Navigating to category (up to {NAV_TIMEOUT_FIRST_MS // 1000}s)…\n"
                f"     {start_url}",
                flush=True,
            )
            initial_network_html: str | None = None
            main_nav_response = await page.goto(
                current_url,
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT_FIRST_MS,
            )
            if save_initial_network_html and main_nav_response and main_nav_response.ok:
                try:
                    initial_network_html = await main_nav_response.text()
                    print(
                        "[plp] Captured first navigation response body for plp_0001_initial_network.html",
                        flush=True,
                    )
                except Exception as e:
                    print(
                        f"[plp] could not read first response body: {e!r}",
                        flush=True,
                    )
            print(
                f"[plp] domcontentloaded; settling {settle_first_sec:.1f}s…",
                flush=True,
            )
            await asyncio.sleep(settle_first_sec)
            current_url = page.url
            if not skip_captcha_flow:
                await resolve_captcha_if_needed(
                    page,
                    plp_reload_url=current_url,
                    min_solve_sec=captcha_min_solve_sec,
                )

            if wait_for_plp_timeout_ms is not None:
                wto = 0 if wait_for_plp_timeout_ms == 0 else wait_for_plp_timeout_ms
                await _wait_for_selector_with_heartbeat(
                    page,
                    PLP_PRODUCT_LINK,
                    wto,
                    "first product link on the listing",
                )
                print("[wait] Product links found — continuing.", flush=True)

            print(
                "[plp] Capturing pages: read DOM (slow) → write plp_0001.html, …; "
                "look for [plp] lines below.",
                flush=True,
            )
            for idx in range(1, max_pages + 1):
                print(
                    f"[plp] Page {idx}/{max_pages}: serializing page source (DOM) — "
                    f"can take 10–90s on very large pages; not stuck",
                    flush=True,
                )
                if use_playwright_page_content:
                    html = await page.content()
                else:
                    try:
                        html = await get_page_source_html(page)
                    except Exception as e:
                        print(
                            f"[plp] get_page_source_html failed ({e!r}) — "
                            f"using page.content() instead",
                            flush=True,
                        )
                        html = await page.content()
                if idx == 1 and initial_network_html is not None:
                    p_net = out_dir / "plp_0001_initial_network.html"
                    p_net.write_text(initial_network_html, encoding="utf-8")
                    print(
                        f"[plp] Wrote {p_net.name} (first HTTP response body; "
                        f"compare to plp_0001.html = live DOM).",
                        flush=True,
                    )
                out_path = out_dir / f"plp_{idx:04d}.html"
                out_path.write_text(html, encoding="utf-8")
                page_pids = extract_pids_from_html(html)
                all_pids |= page_pids
                log_lines.append(
                    f"{idx}\t{page.url}\t{out_path.name}\t{len(page_pids)} pids in page"
                )
                print(
                    f"[{idx}] saved {out_path.name}  ({len(page_pids)} pids on page, "
                    f"{len(all_pids)} unique total)  {time.monotonic() - t0:.1f}s",
                    flush=True,
                )

                if idx >= max_pages:
                    break

                print(
                    f"[plp] Page {idx} saved. Finding next page (Next button or ?page=)…",
                    flush=True,
                )
                clicked = await _try_click_next(page)
                if clicked:
                    await page.wait_for_load_state("domcontentloaded", timeout=60_000)
                    await asyncio.sleep(settle_step_sec)
                    if page.url == current_url:
                        # some builds update list without URL change; still ok
                        pass
                    current_url = page.url
                    if not skip_captcha_flow:
                        await resolve_captcha_if_needed(
                            page,
                            plp_reload_url=current_url,
                            min_solve_sec=captcha_min_solve_sec,
                        )
                    continue

                if use_page_query_fallback:
                    next_n = parse_qs(urlparse(page.url).query).get("page", ["1"])
                    try:
                        cur_page = int(next_n[0]) if next_n else 1
                    except ValueError:
                        cur_page = 1
                    nxt = _next_url_with_page_param(start_url, cur_page + 1)
                    if nxt == page.url:
                        break
                    try:
                        await page.goto(
                            nxt,
                            wait_until="domcontentloaded",
                            timeout=NAV_TIMEOUT_FIRST_MS,
                        )
                        await asyncio.sleep(settle_step_sec)
                        if page.url == current_url and idx > 0:
                            break
                        current_url = page.url
                        if not skip_captcha_flow:
                            await resolve_captcha_if_needed(
                                page,
                                plp_reload_url=current_url,
                                min_solve_sec=captcha_min_solve_sec,
                            )
                        continue
                    except Exception:
                        break
                break

            (out_dir / "plp_crawl_log.txt").write_text(
                "\n".join(log_lines) + "\n", encoding="utf-8"
            )
        finally:
            await browser.close()

    return sorted(all_pids, key=int)


def get_pids(
    categories_url: str,
    *,
    out_dir: Path | None = None,
    max_pages: int = 500,
    headless: bool = False,
    use_chrome: bool = False,
    settle_first_sec: float = 2.0,
    settle_step_sec: float = 1.5,
    use_page_query_fallback: bool = True,
    wait_for_plp_timeout_ms: int | None = None,
    captcha_min_solve_sec: float = MIN_CAPTCHA_SOLVE_SEC_DEFAULT,
    skip_captcha_flow: bool = False,
    use_playwright_page_content: bool = False,
    save_initial_network_html: bool = False,
) -> list[str]:
    """
    Visit ``categories_url``, save each PLP as ``plp_0001.html`` … in ``out_dir``,
    follow Next until it stops or ``max_pages``. Returns sorted unique product ids
    (strings) found in any saved HTML.

    **Captcha (same as url_clicker):** if the page looks like a challenge, a
    notification fires, the script waits ``captcha_min_solve_sec`` (default
    120) so you can solve in the browser, then asks for Enter; if the page
    is still a captcha, it reloads the current listing URL. Optional
    ``wait_for_plp_timeout_ms`` still waits for a product link after that.

    **HTML on disk** is the **live page source** (doctype + ``<html>`` outer
    HTML from the DOM), not a manual browser “Save as”, which often misses
    JS-rendered content. Set ``use_playwright_page_content=True`` to use
    ``page.content()`` instead. With ``save_initial_network_html=True``, the
    first response body is also stored for comparison to the main shell.
    """
    od = out_dir if out_dir is not None else PLP_HTML_DIR
    return asyncio.run(
        _crawl_plp(
            categories_url,
            od,
            max_pages=max_pages,
            headless=headless,
            use_chrome=use_chrome,
            settle_first_sec=settle_first_sec,
            settle_step_sec=settle_step_sec,
            use_page_query_fallback=use_page_query_fallback,
            wait_for_plp_timeout_ms=wait_for_plp_timeout_ms,
            captcha_min_solve_sec=captcha_min_solve_sec,
            skip_captcha_flow=skip_captcha_flow,
            use_playwright_page_content=use_playwright_page_content,
            save_initial_network_html=save_initial_network_html,
        )
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Crawl a SHEIN category PLP: save HTML per page, print unique pids."
    )
    ap.add_argument(
        "--url",
        type=str,
        default=None,
        help="Category URL (default: from category_names[0] / category_ids[0])",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=PLP_HTML_DIR,
        help=f"Output directory (default: {PLP_HTML_DIR})",
    )
    ap.add_argument(
        "--max-pages",
        type=int,
        default=500,
        metavar="N",
        help="Safety cap on PLP pages (default 500)",
    )
    ap.add_argument(
        "--headless",
        action="store_true",
        help="No browser window (automation/CI). Default is a visible Chromium window.",
    )
    ap.add_argument(
        "--headed",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    ap.add_argument(
        "--chrome",
        action="store_true",
        help="Use Google Chrome.app instead of bundled Chromium (often easier to spot on macOS).",
    )
    ap.add_argument(
        "--settle-first",
        type=float,
        default=2.0,
        help="Seconds to wait after first load (default 2)",
    )
    ap.add_argument(
        "--settle-step",
        type=float,
        default=1.5,
        help="Seconds to wait after each Next (default 1.5)",
    )
    ap.add_argument(
        "--no-page-fallback",
        action="store_true",
        help="After Next click fails, do not try ?page=2,3,… on the start URL",
    )
    ap.add_argument(
        "--wait-for-plp-sec",
        type=float,
        default=None,
        metavar="SEC",
        help=(
            "After the first load, wait until a product link is visible. "
            "0 = no time limit. Omit = no selector wait (captcha still uses "
            "url_clicker-style flow if detected)."
        ),
    )
    ap.add_argument(
        "--captcha-min-sec",
        type=float,
        default=MIN_CAPTCHA_SOLVE_SEC_DEFAULT,
        metavar="SEC",
        help=(
            "If a captcha is detected, wait this many seconds in the browser "
            f"before asking for Enter in the terminal (default: "
            f"{MIN_CAPTCHA_SOLVE_SEC_DEFAULT:.0f}, same as url_clicker’s 2m first "
            f"nav budget). Use 0 to only require Enter (like url_clicker with no sleep)."
        ),
    )
    ap.add_argument(
        "--no-captcha-interactive",
        action="store_true",
        help="Do not run captcha notification / sleep / Enter / reload (automation).",
    )
    ap.add_argument(
        "--playwright-content",
        action="store_true",
        help="Use page.content() for HTML (Playwright default) instead of live DOM outerHTML.",
    )
    ap.add_argument(
        "--save-initial-network-html",
        action="store_true",
        help=(
            "Also write plp_0001_initial_network.html: body of the first main navigation "
            "(may be a small shell on SPAs; plp_0001.html is the full live DOM you want for pids)."
        ),
    )
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    url = args.url
    if not url:
        url = get_categories_url(category_names[0], category_ids[0])
        print(f"[default url] {url}", flush=True)
    # Visible window by default so captcha / PLP can be solved in a real tab.
    # --headed kept as a no-op for old invocations (visible is default).
    run_headless = bool(args.headless) and not args.headed
    if run_headless and not args.no_captcha_interactive:
        print(
            "[warn] --headless: no browser tab will appear. "
            "Omit --headless to see Chromium, or use --no-captcha-interactive if intentional.",
            flush=True,
        )

    wait_ms: int | None = None
    if args.wait_for_plp_sec is not None:
        wait_ms = 0 if args.wait_for_plp_sec == 0 else int(
            max(args.wait_for_plp_sec, 0) * 1000
        )

    pids = get_pids(
        url,
        out_dir=args.out,
        max_pages=args.max_pages,
        headless=run_headless,
        use_chrome=args.chrome,
        settle_first_sec=args.settle_first,
        settle_step_sec=args.settle_step,
        use_page_query_fallback=not args.no_page_fallback,
        wait_for_plp_timeout_ms=wait_ms,
        captcha_min_solve_sec=args.captcha_min_sec,
        skip_captcha_flow=args.no_captcha_interactive,
        use_playwright_page_content=args.playwright_content,
        save_initial_network_html=args.save_initial_network_html,
    )
    out_pids = args.out / "pids.txt"
    out_pids.write_text("\n".join(pids) + ("\n" if pids else ""), encoding="utf-8")
    print(f"\n[done] {len(pids)} unique pids  →  {out_pids}", flush=True)


if __name__ == "__main__":
    main()
