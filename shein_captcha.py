"""
shein_captcha.py
================
Auto-solver for SHEIN's /risk/challenge captcha (captcha_type=909).

Two observed sequential variants:

  1. Checkbox  – modal says "Please click to complete … I am human"
                 Action: click the checkbox widget with realistic timing.

  2. Image-seq – modal says "Please select the following graphics in order:"
                 Top row: 2-3 reference file icons (RTF / XLS / CSV / DOC …)
                 Scene:   those icons overlaid at random positions in a photo
                 Action:  click each icon in the scene IN the top-row order → Confirm

Checkbox is handled with pure Playwright DOM interaction.
Image-seq is handled by:
  1. Taking a screenshot of the challenge modal.
  2. Calling the OpenAI GPT-4o vision API (or another configured backend) to identify
     where the reference icons appear in the scene and return click coordinates.
  3. Clicking those coordinates in order, then clicking Confirm.

If no API key is set, or if auto-solve fails, the code falls back to the existing
manual flow (pause + Enter in the terminal).

Environment variables:
    OPENAI_API_KEY         – required for image-seq auto-solve
    SHEIN_CAPTCHA_DEBUG=1  – save each captcha screenshot to captcha_debug/
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import time
from pathlib import Path
from typing import Literal

# ── debug ──────────────────────────────────────────────────────────────────

_DEBUG = bool(os.environ.get("SHEIN_CAPTCHA_DEBUG"))
_DEBUG_DIR = Path(__file__).parent / "captcha_debug"

# ── variant detection ──────────────────────────────────────────────────────

CaptchaVariant = Literal["none", "checkbox", "image_seq"]

_CHECKBOX_KEYWORDS = ["i am human", "please click to complete"]
_IMAGE_SEQ_KEYWORDS = [
    "select the following graphics in order",
    "select the following graphics",
    "please select the following",
]


async def detect_captcha_variant(page) -> CaptchaVariant:
    """
    Return ``'checkbox'``, ``'image_seq'``, or ``'none'``.
    Does NOT call ``page.content()`` for the ``'none'`` fast-path.
    """
    try:
        url = page.url
    except Exception:
        return "none"
    if "risk/challenge" not in url:
        # Quick DOM-free check first
        try:
            title = await page.title()
        except Exception:
            title = ""
        if "risk" not in title.lower() and "challenge" not in title.lower():
            return "none"

    # Need page content for deeper check
    try:
        content = (await page.content()).lower()
    except Exception:
        return "none"

    if any(k in content for k in _IMAGE_SEQ_KEYWORDS):
        return "image_seq"
    if any(k in content for k in _CHECKBOX_KEYWORDS):
        return "checkbox"
    if "risk/challenge" in url:
        # Challenge page whose text hasn't loaded yet; do NOT assume checkbox —
        # it might be direct image_seq.  Return "none" so the caller re-polls.
        return "none"
    return "none"


# ── checkbox solver ────────────────────────────────────────────────────────

# Ordered by specificity; first visible match wins.
_CHECKBOX_SELECTORS = [
    ".verify-wrap .checkbox-item",
    ".verify-checkbox",
    ".captcha-checkbox",
    "label:has-text('I am human')",
    "span:has-text('I am human')",
    "[class*='checkbox']",
    "input[type='checkbox']",
    # Broad fallback: any clickable element in the modal near the text
    ".verify-wrap",
]


async def _human_click(page, locator) -> bool:
    """Move mouse to element with slight random offset and click. Returns True on success."""
    try:
        box = await locator.bounding_box()
        if box and box["width"] > 0:
            cx = box["x"] + box["width"] * random.uniform(0.3, 0.6)
            cy = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            # approach from a slight offset
            await page.mouse.move(
                cx + random.uniform(-30, 30),
                cy + random.uniform(-20, 20),
                steps=random.randint(6, 14),
            )
            await asyncio.sleep(random.uniform(0.08, 0.20))
            await page.mouse.move(cx, cy, steps=random.randint(3, 7))
            await asyncio.sleep(random.uniform(0.10, 0.25))
            await page.mouse.click(cx, cy)
            return True
    except Exception:
        pass
    try:
        await locator.click(timeout=3000)
        return True
    except Exception:
        return False


async def solve_checkbox(page) -> bool:
    """
    Click the 'I am human' checkbox/widget.
    Returns True if the click was issued (not necessarily verified solved).
    """
    for sel in _CHECKBOX_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1500):
                ok = await _human_click(page, loc)
                if ok:
                    print("  [captcha] clicked checkbox widget.", flush=True)
                    return True
        except Exception:
            continue
    print("  [captcha] checkbox: no matching element found.", flush=True)
    return False


# ── image-seq solver ───────────────────────────────────────────────────────

_MODAL_SELECTORS = [
    ".captcha-verify-wrap",
    ".verify-wrap",
    "[class*='captcha-modal']",
    "[class*='verify-modal']",
    "[class*='captcha-wrap']",
    "[role='dialog']",
    "dialog",
]

_CONFIRM_SELECTORS = [
    "button:has-text('Confirm')",
    ".captcha-submit",
    "[class*='confirm-btn']",
    "button[class*='confirm']",
    "[class*='submit']",
]

_REFRESH_SELECTORS = [
    "button:has-text('Refresh')",
    "[class*='refresh']",
    ".captcha-refresh",
    "button:has-text('refresh')",
]

# ── two-step agent prompts ─────────────────────────────────────────────────

# Step 1: identify WHAT the reference icons are (text description only)
_AGENT_STEP1_PROMPT = """
You are looking at a screenshot of a SHEIN security captcha.

At the TOP of the modal there is a horizontal strip showing 2–3 small file-type icons
in a specific left-to-right order (e.g. RTF, XLS, CSV, DOC, TXT).

Task: Describe each reference icon in that top strip, LEFT TO RIGHT.
Be very specific — include the file-type label visible on each icon (RTF, XLS, CSV, etc.).

Return ONLY a JSON array of short label strings, e.g.:
  ["RTF", "XLS", "DOC"]

No explanations, no markdown fences.
""".strip()

# Step 2: given the icon labels, find their positions in the scene image
_AGENT_STEP2_PROMPT_TMPL = """
You are looking at a screenshot of a SHEIN security captcha.

The modal has:
  • TOP ROW: reference icons in this order: {labels}
  • SCENE IMAGE: a large photograph (e.g. night sky, landscape) with those same
    file-type icons overlaid / scattered at various pixel positions.

Task: Find each reference icon from the top row INSIDE the scene image, in the order listed.
Return ONLY a JSON array of pixel coordinates, one per icon, in order:

  [{{"x": 412, "y": 291}}, {{"x": 503, "y": 195}}, {{"x": 467, "y": 350}}]

Rules:
  • x / y are pixel positions relative to the TOP-LEFT of the entire screenshot.
  • Output ONLY the JSON array — no explanation, no markdown fences.
  • If you cannot locate an icon with confidence, return [] for the whole array.
""".strip()


async def _screenshot_modal(page) -> tuple[bytes, dict | None]:
    """
    Full-viewport screenshot (coords stay absolute). Also returns the modal
    bounding box for reference, or None if not found.
    """
    box: dict | None = None
    for sel in _MODAL_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1200):
                b = await loc.bounding_box()
                if b and b["width"] > 80 and b["height"] > 80:
                    box = b
                    break
        except Exception:
            continue
    png = await page.screenshot(type="png", full_page=False)
    return png, box


def _openai_chat(api_key: str, messages: list[dict], max_tokens: int = 256) -> str:
    """
    Synchronous OpenAI chat/completions call. Returns the raw text content.
    Run via loop.run_in_executor.
    """
    import urllib.request

    payload = json.dumps(
        {
            "model": "gpt-4o",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0,
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        body = json.loads(resp.read())
    return body["choices"][0]["message"]["content"].strip()


def _strip_fences(text: str) -> str:
    """Remove markdown code fences that some models add around JSON."""
    t = text.strip()
    if t.startswith("```"):
        parts = t.split("```")
        # parts[1] is the content between the first pair of fences
        inner = parts[1] if len(parts) > 1 else t
        if inner.startswith("json"):
            inner = inner[4:]
        return inner.strip()
    return t


def _agent_solve_captcha(api_key: str, png_bytes: bytes) -> list[dict]:
    """
    Two-step agent loop (blocking, run in executor):

      Step 1 — Ask: "What are the reference icons in order?" → list of labels
      Step 2 — Ask: "Given those labels, find their coords in the scene" → click coords

    Returns list of {x, y} dicts, empty on failure.
    """
    b64 = base64.b64encode(png_bytes).decode()
    image_content = {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
    }

    # ── Step 1: identify the reference icons ──────────────────────────────
    step1_messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _AGENT_STEP1_PROMPT},
                image_content,
            ],
        }
    ]
    raw1 = _openai_chat(api_key, step1_messages, max_tokens=128)
    try:
        labels: list[str] = json.loads(_strip_fences(raw1))
        if not isinstance(labels, list) or not labels:
            raise ValueError("empty or non-list")
        labels = [str(l).strip().upper() for l in labels]
    except Exception as exc:
        raise RuntimeError(f"Step 1 parse error ({exc!r}): {raw1!r}") from exc

    label_str = ", ".join(labels)

    # ── Step 2: locate the icons in the scene ─────────────────────────────
    step2_prompt = _AGENT_STEP2_PROMPT_TMPL.format(labels=label_str)
    step2_messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": step2_prompt},
                image_content,
            ],
        }
    ]
    raw2 = _openai_chat(api_key, step2_messages, max_tokens=256)
    coords: list[dict] = json.loads(_strip_fences(raw2))
    if not isinstance(coords, list):
        return []
    return [
        {"x": int(c["x"]), "y": int(c["y"])}
        for c in coords
        if isinstance(c, dict) and "x" in c and "y" in c
    ]


async def _try_refresh(page) -> None:
    for sel in _REFRESH_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=800):
                await loc.click()
                print("  [captcha] clicked Refresh.", flush=True)
                return
        except Exception:
            continue


async def solve_image_seq(page, api_key: str) -> bool:
    """
    Two-step agent solve of the image-sequence variant:
      Step 1 — capture screenshot, ask AI to identify the reference icons.
      Step 2 — ask AI to locate those icons in the scene and return click coords.
    Single attempt — no retry. Returns True if captcha is gone after solving.
    """
    loop = asyncio.get_event_loop()

    # --- capture ---
    print("  [captcha] agent: capturing captcha screenshot …", flush=True)
    try:
        png, _box = await _screenshot_modal(page)
    except Exception as exc:
        print(f"  [captcha] screenshot failed: {exc}", flush=True)
        return False

    if _DEBUG:
        _DEBUG_DIR.mkdir(exist_ok=True)
        ts = int(time.time())
        path = _DEBUG_DIR / f"captcha_{ts}.png"
        path.write_bytes(png)
        print(f"  [captcha] debug screenshot → {path}", flush=True)

    # --- two-step agent ---
    print("  [captcha] agent: step 1 — identifying reference icons …", flush=True)
    try:
        coords = await loop.run_in_executor(None, _agent_solve_captcha, api_key, png)
    except Exception as exc:
        print(f"  [captcha] agent error: {exc}", flush=True)
        return False

    if not coords:
        print(
            "  [captcha] agent could not locate the icons — manual solve needed.",
            flush=True,
        )
        return False

    print(f"  [captcha] agent step 2 — clicking {len(coords)} icon(s): {coords}", flush=True)

    # --- click icons in order ---
    for ci, c in enumerate(coords):
        px = float(c["x"]) + random.uniform(-2, 2)
        py = float(c["y"]) + random.uniform(-2, 2)
        await page.mouse.move(
            px + random.uniform(-20, 20),
            py + random.uniform(-15, 15),
            steps=random.randint(8, 18),
        )
        await asyncio.sleep(random.uniform(0.25, 0.55))
        await page.mouse.move(px, py, steps=random.randint(3, 6))
        await asyncio.sleep(random.uniform(0.15, 0.30))
        await page.mouse.click(px, py)
        print(
            f"  [captcha]   clicked icon {ci + 1}/{len(coords)} at ({px:.0f}, {py:.0f})",
            flush=True,
        )
        await asyncio.sleep(random.uniform(0.40, 0.80))

    # --- click Confirm ---
    confirmed = False
    for sel in _CONFIRM_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1200):
                await loc.click()
                confirmed = True
                print("  [captcha] clicked Confirm.", flush=True)
                break
        except Exception:
            continue
    if not confirmed:
        print("  [captcha] Confirm button not found — manual solve needed.", flush=True)
        return False

    await asyncio.sleep(random.uniform(1.8, 2.5))

    variant = await detect_captcha_variant(page)
    if variant == "none":
        print("  [captcha] image-seq solved ✓", flush=True)
        return True

    print(
        f"  [captcha] image-seq: still showing ({variant}) after attempt — manual solve needed.",
        flush=True,
    )
    return False


# ── main entrypoint ────────────────────────────────────────────────────────


# Maximum time to wait for the captcha modal to become visible.
# Once visible (and variant detected), we proceed immediately — no extra fixed wait.
CAPTCHA_PAGE_SETTLE_SEC = 60.0

# Selectors that indicate the challenge modal has fully rendered.
_READY_SELECTORS = [
    # image-seq signals (prioritise — more specific)
    "button:has-text('Confirm')",
    "button:has-text('Refresh')",
    "[class*='captcha-verify-wrap']",
    # checkbox signals
    "label:has-text('I am human')",
    "span:has-text('I am human')",
    "[class*='verify-wrap']",
    "[role='dialog']",
]


async def _wait_for_captcha_ready(
    page, max_wait_sec: float = CAPTCHA_PAGE_SETTLE_SEC
) -> CaptchaVariant:
    """
    Poll until the challenge modal is fully rendered (a key element is visible
    AND ``detect_captcha_variant`` returns a known variant), then return immediately.
    Gives up after ``max_wait_sec`` and returns whatever variant was last seen.
    """
    start = time.monotonic()
    deadline = start + max_wait_sec
    last_variant: CaptchaVariant = "none"

    while time.monotonic() < deadline:
        # Check for a visible modal element first (cheap DOM check)
        element_ready = False
        for sel in _READY_SELECTORS:
            try:
                if await page.locator(sel).first.is_visible(timeout=400):
                    element_ready = True
                    break
            except Exception:
                pass

        if element_ready:
            # Element is visible — confirm the variant from page text
            variant = await detect_captcha_variant(page)
            if variant != "none":
                elapsed = time.monotonic() - start
                print(
                    f"  [captcha] modal ready after {elapsed:.0f}s — variant: {variant}",
                    flush=True,
                )
                return variant
            last_variant = variant

        await asyncio.sleep(2.0)

    elapsed = time.monotonic() - start
    print(
        f"  [captcha] modal not fully ready after {elapsed:.0f}s — proceeding with last known variant.",
        flush=True,
    )
    return last_variant or await detect_captcha_variant(page)


async def auto_solve_captcha(
    page,
    openai_api_key: str | None = None,
    manual_fallback_fn=None,
    max_wait_sec: float = CAPTCHA_PAGE_SETTLE_SEC,
) -> bool:
    """
    Attempt full auto-solve of a SHEIN /risk/challenge captcha.

    Flow:
      0. Poll until the challenge modal is ready (up to ``max_wait_sec``).
         Proceeds as soon as the modal is visible — no unnecessary extra wait.
      1. ``checkbox``  → click → re-detect.
      2. ``image_seq`` → one vision-model attempt → if it fails, immediately
         fall back to manual (no retries).
      3. If manual_fallback_fn provided, call it and wait for human.

    Returns True if no captcha remains, False otherwise.
    """
    api_key = openai_api_key or os.environ.get("OPENAI_API_KEY") or ""

    # Quick exit if not a challenge page at all
    if "risk/challenge" not in page.url:
        initial = await detect_captcha_variant(page)
        if initial == "none":
            return True

    # Step 0: wait until the modal is actually rendered (stops as soon as ready)
    print("  [captcha] challenge page — waiting for modal to render …", flush=True)
    variant = await _wait_for_captcha_ready(page, max_wait_sec)

    if variant == "none":
        print("  [captcha] challenge cleared itself while waiting.", flush=True)
        return True

    # Step 1: checkbox (only if that's what's showing)
    if variant == "checkbox":
        print("  [captcha] checkbox variant — clicking …", flush=True)
        await solve_checkbox(page)
        await asyncio.sleep(random.uniform(1.5, 2.5))
        variant = await detect_captcha_variant(page)
        if variant == "none":
            print("  [captcha] checkbox cleared ✓", flush=True)
            return True
        print(f"  [captcha] after checkbox → variant: {variant}", flush=True)
        if variant == "image_seq":
            # Small settle for the image-seq modal to finish rendering
            await asyncio.sleep(random.uniform(1.5, 2.5))

    # Step 2: image-seq — single attempt, no retry
    if variant == "image_seq":
        if api_key:
            ok = await solve_image_seq(page, api_key)
            if ok:
                return True
            # auto-solve failed — fall through to manual immediately
            print(
                "  [captcha] auto-solve failed — manual solve needed.",
                flush=True,
            )
        else:
            print(
                "  [captcha] image-seq detected but OPENAI_API_KEY not set.\n"
                "           Pass --openai-key or set OPENAI_API_KEY for auto-solve.\n"
                "           Manual solve needed.",
                flush=True,
            )

    # Step 3: manual fallback
    if manual_fallback_fn is not None:
        await manual_fallback_fn(page)
        await asyncio.sleep(1.5)
        return await detect_captcha_variant(page) == "none"

    return False
