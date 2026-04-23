"""
SHEIN PDP: **color variants only** (product keys in ``allColorDetailImages``).

Sizes / ``sku_list`` exist in the page JSON but are **out of scope** here — extract them later
elsewhere if needed.

**Preferred:** ``window.gbRawData`` → keys of ``allColorDetailImages`` → one PDP per color
(``-p-{product_key}.html``), saved as flat ``pages/<product_key>.html``.

**Fallback:** click color swatches under ``.main-sales-attr__color-container`` only (no size clicks).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import NamedTuple


class VariantScrapeResult(NamedTuple):
    """``extra_saved`` = additional HTML files written; ``waf_block`` = variant navigation hit a WAF/403 page."""

    extra_saved: int
    waf_block: bool

# Try in order until some nodes match (US desktop PDP).
# Color strip: everything lives under .main-sales-attr__color-container (click radios, not URLs).
COLOR_SELECTORS = [
    ".main-sales-attr__color-container div.radio-container[role='radio']",
    ".main-sales-attr__color-container [role='radio']",
    ".main-sales-attr__color-container .bs-attr-crop-image-container.fsp-element",
    ".main-sales-attr__color .bs-attr-crop-image-container",
    ".product-intro__color-sku .product-intro__color-item",
    ".product-intro__color-sku .j-list-item",
    ".product-intro__color-sku button",
    "[class*='color-sku'] [class*='color-item']",
    # Narrow fallback: swatch images inside the color strip only (not gallery thumbs)
    ".main-sales-attr__color-container img.bs-attr-crop-image-container__img",
]

SETTLE_MS = 900
# After each variant ``goto``; gbRawData is often not yet in the first paint
SETTLE_AFTER_GOTO_SEC = 2.5

_RE_P_GOODS = re.compile(r"(-p-)(\d+)(\.html[^#?]*)", re.IGNORECASE)


def parse_gb_raw_data(html: str) -> dict | None:
    """
    Parse ``window.gbRawData = {...}`` from server-rendered HTML (JSON object).
    """
    m = re.search(r"window\.gbRawData\s*=\s*", html)
    if not m:
        m = re.search(r"\bgbRawData\s*=\s*", html)
    if not m:
        return None
    start = m.end()
    chunk = html[start : start + 1_500_000].lstrip("\ufeff \t\n\r")
    if not chunk.startswith("{"):
        return None
    depth = 0
    for i, c in enumerate(chunk):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(chunk[: i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def shein_pdp_parseable_gb_raw_data(html: str) -> bool:
    """
    True if the page contains a full parse of ``window.gbRawData`` JSON.
    Missing or invalid blob usually means a block / error HTML (e.g. 403 WAF) rather
    than a real US PDP snapshot.
    """
    return parse_gb_raw_data(html) is not None


async def _html_with_parseable_gb(page) -> str | None:
    """Wait for content that still contains a parseable ``gbRawData`` (retry a few times)."""
    for attempt in range(4):
        await asyncio.sleep(SETTLE_AFTER_GOTO_SEC if attempt == 0 else 0.8 + attempt * 0.4)
        html = await page.content()
        if parse_gb_raw_data(html):
            return html
    html = await page.content()
    return html if parse_gb_raw_data(html) else None


def pdp_url_replace_goods_id(url: str, new_goods_id: str) -> str:
    """Replace ``...-p-OLD.html`` with ``...-p-NEW.html`` (same slug, different goods / product key)."""
    if _RE_P_GOODS.search(url):
        return _RE_P_GOODS.sub(rf"\g<1>{new_goods_id}\g<3>", url, count=1)
    return url


def product_keys_from_all_color_detail_images(aci: object) -> list[str]:
    """
    ``allColorDetailImages`` maps **product key** (``goods_id`` string) → list of image dicts.
    Return ordered keys for iteration (same order as in the JSON object).
    """
    if not isinstance(aci, dict) or not aci:
        return []
    return [str(k) for k in aci.keys()]


def goods_id_from_pdp_url(url: str) -> str | None:
    m = _RE_P_GOODS.search(url)
    return m.group(2) if m else None


def _intish(v: object) -> int | None:
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def availability_from_gb_raw_data(gb: dict | None) -> dict | None:
    """
    Stock / sold-out from ``modules.productInfo`` (same fields SHEIN uses on the PDP).

    Reference pages: sold-out has ``stock`` 0 and ``is_on_sale`` 0; in-stock has
    positive ``stock`` and ``is_on_sale`` 1. ``is_on_sale`` here tracks availability
    for the listing, not “discount sale” only.
    """
    if not gb:
        return None
    pi = (gb.get("modules") or {}).get("productInfo") or {}
    if not pi:
        return None
    gid = str(pi.get("goods_id") or "")
    stock = _intish(pi.get("stock"))
    is_on_sale = _intish(pi.get("is_on_sale"))
    # Whole-PDP sold out when aggregate stock is 0 (matches 433580043 vs 433646970 samples).
    sold_out = stock == 0 if stock is not None else None
    return {
        "goods_id": gid,
        "stock": stock,
        "is_on_sale": is_on_sale,
        "sold_out": sold_out,
    }


def write_availability_json(pages_dir: Path, pid: str, html: str) -> dict | None:
    """
    Write ``pages/<pid>.availability.json`` next to the HTML. Returns the dict if written.
    """
    av = availability_from_gb_raw_data(parse_gb_raw_data(html))
    if not av:
        return None
    pages_dir.mkdir(parents=True, exist_ok=True)
    path = pages_dir / f"{pid}.availability.json"
    path.write_text(json.dumps(av, ensure_ascii=False, indent=2), encoding="utf-8")
    return av


async def _scrape_variants_from_gbdata(
    page,
    product_id: str,
    pages_dir: Path,
    pdp_url: str,
    initial_gb: dict,
) -> VariantScrapeResult:
    """
    Iterate **keys** of ``allColorDetailImages`` as product keys (``goods_id`` per color PDP);
    open each ``-p-{product_key}.html`` URL. Saves only flat ``pages/<product_key>.html``.
    The current PDP is already ``pages/<product_id>.html`` when ``product_key == product_id``.
    """
    pi = (initial_gb.get("modules") or {}).get("productInfo") or {}
    aci = pi.get("allColorDetailImages")
    product_keys = product_keys_from_all_color_detail_images(aci)
    if not product_keys:
        return VariantScrapeResult(0, False)

    base_url = (pdp_url or "").strip() or page.url
    cur_key = goods_id_from_pdp_url(base_url)
    if cur_key and cur_key in product_keys:
        product_keys = [cur_key] + [k for k in product_keys if k != cur_key]

    pages_dir.mkdir(parents=True, exist_ok=True)
    count = 0

    for product_key in product_keys:
        target = pdp_url_replace_goods_id(base_url, product_key)
        cur = goods_id_from_pdp_url(page.url)
        if cur != product_key:
            try:
                await page.goto(
                    target,
                    wait_until="domcontentloaded",
                    timeout=90_000,
                )
            except Exception as exc:
                print(
                    f"  [variants] {product_key}: goto failed: {exc!s}",
                    flush=True,
                )
                continue
            html = await _html_with_parseable_gb(page)
            if not html or not parse_gb_raw_data(html):
                print(
                    f"  [variants] {product_key}: no parseable gbRawData after load",
                    flush=True,
                )
                return VariantScrapeResult(count, True)
        else:
            html = await page.content()
            if not parse_gb_raw_data(html):
                html2 = await _html_with_parseable_gb(page)
                if not html2 or not parse_gb_raw_data(html2):
                    print(
                        f"  [variants] {product_key}: gbRawData not parseable on current page",
                        flush=True,
                    )
                    return VariantScrapeResult(count, True)
                html = html2

        # Main run already wrote pages/<product_id>.html for this URL's product key.
        if product_key == str(product_id):
            continue

        out = pages_dir / f"{product_key}.html"
        out.write_text(html, encoding="utf-8")
        write_availability_json(pages_dir, product_key, html)
        count += 1

    if pdp_url:
        try:
            if goods_id_from_pdp_url(page.url) != goods_id_from_pdp_url(pdp_url):
                await page.goto(
                    pdp_url,
                    wait_until="domcontentloaded",
                    timeout=90_000,
                )
                await asyncio.sleep(0.3)
        except Exception:
            pass

    return VariantScrapeResult(count, False)


async def _visible_locators(page, selector: str, limit: int = 40):
    loc = page.locator(selector)
    n = await loc.count()
    out = []
    for i in range(min(n, limit)):
        el = loc.nth(i)
        try:
            if await el.is_visible():
                out.append(el)
        except Exception:
            continue
    return out


async def find_color_locators(page):
    for sel in COLOR_SELECTORS:
        found = await _visible_locators(page, sel)
        if found:
            return found
    return []


async def scrape_variant_matrix(
    page,
    product_id: str,
    pages_dir: Path,
    *,
    pdp_url: str | None = None,
) -> VariantScrapeResult:
    """
    **Color variants only** (see module doc). Prefer ``allColorDetailImages`` keys → flat
    ``pages/<product_key>.html`` for each *other* color PDP. DOM fallback: one
    ``pages/<product_id>_color{ci}.html`` per extra color (index ``ci >= 1``; ``ci==0`` is the
    main PDP already saved as ``pages/<product_id>.html``). Returns count of **extra** HTML
    files; ``waf_block`` is set when a variant navigation looks like 403/WAF (no parseable
    ``gbRawData``) — the caller should end the session like a main-PDP block.
    """
    await asyncio.sleep(SETTLE_MS / 1000)

    base = (pdp_url or "").strip()
    if not base:
        try:
            base = page.url
        except Exception:
            base = ""

    html = await page.content()
    gb = parse_gb_raw_data(html)
    pi = (gb or {}).get("modules", {}).get("productInfo") or {}
    aci = pi.get("allColorDetailImages")

    if gb and isinstance(aci, dict) and len(aci) > 0 and base:
        return await _scrape_variants_from_gbdata(
            page, product_id, pages_dir, base, gb
        )

    try:
        color_strip = page.locator(".main-sales-attr__color-container").first
        await color_strip.scroll_into_view_if_needed(timeout=8_000)
    except Exception:
        pass
    await asyncio.sleep(0.45)

    colors = await find_color_locators(page)
    if len(colors) <= 1:
        return VariantScrapeResult(0, False)

    pages_dir.mkdir(parents=True, exist_ok=True)
    count = 0

    # Color index 0 is already the loaded PDP (pages/<product_id>.html). Only other colors.
    for ci in range(1, len(colors)):
        cur_colors = await find_color_locators(page)
        if ci >= len(cur_colors):
            break
        try:
            await cur_colors[ci].click(timeout=10_000)
            await asyncio.sleep(SETTLE_MS / 1000)
        except Exception as exc:
            print(
                f"  [variants] DOM color index {ci}: click failed: {exc!s}",
                flush=True,
            )
            continue

        html = await page.content()
        stem = f"{product_id}_color{ci}"
        html_path = pages_dir / f"{stem}.html"
        html_path.write_text(html, encoding="utf-8")
        write_availability_json(pages_dir, stem, html)
        count += 1

    return VariantScrapeResult(count, False)
