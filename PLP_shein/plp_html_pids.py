"""
Extract SHEIN product ids from saved PLP HTML where each tile exposes ``data-id="<goods_id>"``.

Works on files like ``pages/n_com_Food_Beverages_c_13086_html_page_1.html``.
Also matches ``-p-<id>.html`` in links as a fallback when ``data-id`` is missing.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

# Product grid cards: data-id="376311558" (digits = goods / product id)
DATA_ID_ATTR = re.compile(
    r"""data-id=["'](\d{5,18})["']""",
    re.IGNORECASE,
)
# Fallback: href="...-p-376311558.html"
PID_FROM_HREF = re.compile(
    r"-p-(\d{5,18})\.html",
    re.IGNORECASE,
)


def extract_product_ids_from_plp_html(html: str) -> list[str]:
    """
    Return sorted unique product id strings from ``data-id`` attributes; if none
    found, fall back to ids from ``-p-<id>.html`` URLs in the same HTML.
    """
    seen: set[str] = set()
    for m in DATA_ID_ATTR.finditer(html):
        seen.add(m.group(1))
    if seen:
        return sorted(seen, key=int)
    for m in PID_FROM_HREF.finditer(html):
        seen.add(m.group(1))
    return sorted(seen, key=int)


def extract_product_ids_from_plp_file(path: str | Path) -> list[str]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return extract_product_ids_from_plp_html(text)


def extract_product_ids_from_plp_files(paths: list[str | Path]) -> list[str]:
    """Merge ids from several saved pages, unique, sorted."""
    all_ids: set[str] = set()
    for p in paths:
        all_ids.update(extract_product_ids_from_plp_file(p))
    return sorted(all_ids, key=int)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Extract product ids from SHEIN PLP HTML (data-id on product cards)."
    )
    ap.add_argument(
        "files",
        nargs="+",
        type=Path,
        help="One or more saved .html files (e.g. n_com_*_page_1.html)",
    )
    ap.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="Write one id per line to this file (default: print to stdout)",
    )
    args = ap.parse_args()
    pids = extract_product_ids_from_plp_files(args.files)
    if args.out:
        args.out.write_text("\n".join(pids) + ("\n" if pids else ""), encoding="utf-8")
        print(f"[plp_html_pids] {len(pids)} ids → {args.out}", flush=True)
    else:
        for pid in pids:
            print(pid)


if __name__ == "__main__":
    main()
