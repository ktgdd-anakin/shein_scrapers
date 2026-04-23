"""
Geonode (or generic) sticky proxies: load list, persist cooldown state, helpers
for Playwright incognito contexts with one proxy per session.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
import re
from urllib.parse import quote, urlparse, unquote


def build_geonode_sticky_proxy_url(
    geonode_username: str,
    password: str,
    country_code: str,
    session_id: str,
    *,
    host: str = "proxy.geonode.io",
    port: int = 10000,
) -> str:
    """
    Build ``http://user:pass@host:port`` for Geonode sticky sessions.

    Geonode expects the *proxy* endpoint (e.g. ``http://proxy.geonode.io:PORT``) and a
    username of the form::

        <geonode_username>-country-<country_code>-session-<session_id>
        (e.g. ``-country-us-`` for United States — use the code Geonode lists for your product.)

    Reuse the same ``session_id`` to keep the same egress IP (sticky).  ``session_id``
    must be 1–25 characters: letters, digits, underscores only.

    The *target* URL you scrape (SHEIN, etc.) is **not** this host — it is passed to
    Playwright/curl as the normal request URL; the proxy only tunnels traffic.

    ``geonode_username`` is often an email: the ``@`` is URL-encoded in the final URL.

    **Port / product:** Geonode docs show **sticky HTTP** on ports **10000–10900** and
    **SOCKS5** on **12000–12010**; generic examples sometimes use **9000**. Always copy
    **host and port** from your Geonode dashboard for your exact product. Playwright
    uses an **HTTP** proxy — use an **HTTP** port, not a SOCKS-only port, unless you
    switch client config.
    """
    sid = session_id.strip()
    if not re.fullmatch(r"[a-zA-Z0-9_]{1,25}", sid):
        raise ValueError(
            "session_id must be 1–25 chars [a-zA-Z0-9_] (Geonode sticky rules)"
        )
    cc = country_code.strip().lower()
    user = f"{geonode_username}-country-{cc}-session-{sid}"
    # Encode userinfo so @ in email and symbols in password are safe.
    uq = quote(user, safe="")
    pq = quote(password, safe="")
    return f"http://{uq}:{pq}@{host}:{port}"


def mask_proxy_url(proxy_url: str) -> str:
    """Redact password for CSV / terminal logging."""
    u = urlparse(proxy_url.strip())
    if not u.hostname:
        return proxy_url
    scheme = u.scheme or "http"
    port = f":{u.port}" if u.port else ""
    if u.username:
        return f"{scheme}://{u.username}:***@{u.hostname}{port}"
    return f"{scheme}://{u.hostname}{port}"

# Wall-clock window per proxy session: session ends at this time or on login (no per-session product cap).
SESSION_WORK_SEC = 600  # 10 minutes
# Max concurrent browser tabs in one context (stability cap; not a per-session URL limit).
MAX_TABS_PER_SESSION = 4
# After a session ends (time limit or login), do not reuse proxy until then.
COOLDOWN_SEC = 3600  # 1 hour

DEFAULT_PROXY_FILE = "geonode_proxies.txt"
STATE_FILENAME = "proxy_state.json"


def load_proxy_lines(path: Path) -> list[str]:
    """One proxy URL per line; # and blank lines ignored."""
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def parse_playwright_proxy(proxy_url: str) -> dict:
    """
    Geonode URLs look like http://user:pass@host:port
    Playwright wants server + username + password separately.
    """
    u = urlparse(proxy_url.strip())
    if not u.hostname:
        raise ValueError(f"Invalid proxy URL (no host): {proxy_url!r}")
    scheme = u.scheme or "http"
    port = u.port
    if port is None:
        port = 443 if scheme == "https" else 80
    server = f"{scheme}://{u.hostname}:{port}"
    user = unquote(u.username) if u.username else ""
    password = unquote(u.password) if u.password else ""
    return {"server": server, "username": user, "password": password}


class ProxyState:
    """Persist cooldown_until per proxy URL in STATE_FILENAME."""

    def __init__(self, base_dir: Path):
        self.path = base_dir / STATE_FILENAME
        self.entries: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = data.get("entries") or {}
        except (json.JSONDecodeError, OSError):
            self.entries = {}

    def save(self) -> None:
        self.path.write_text(
            json.dumps({"entries": self.entries}, indent=2),
            encoding="utf-8",
        )

    def cooldown_until(self, proxy_url: str) -> float:
        key = proxy_url.strip()
        ent = self.entries.get(key)
        if not ent:
            return 0.0
        return float(ent.get("cooldown_until") or 0)

    def is_available(self, proxy_url: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return now >= self.cooldown_until(proxy_url)

    def mark_cooldown(self, proxy_url: str, reason: str) -> None:
        key = proxy_url.strip()
        until = time.time() + COOLDOWN_SEC
        self.entries[key] = {
            "cooldown_until": until,
            "reason": reason,
            "marked_at": time.time(),
        }
        self.save()



def pick_next_proxy(proxies: list[str], state: ProxyState) -> str | None:
    """First proxy in list order that is not in cooldown."""
    now = time.time()
    for p in proxies:
        if state.is_available(p, now):
            return p
    return None


def wait_seconds_for_next_proxy(state: ProxyState, proxies: list[str]) -> float:
    """Seconds until the earliest proxy leaves cooldown (0 if one is ready now)."""
    now = time.time()
    waits: list[float] = []
    for p in proxies:
        cu = state.cooldown_until(p)
        if cu > now:
            waits.append(cu - now)
    if not waits:
        return 0.0
    return max(0.0, min(waits))


# Login / sign-in page detection (URL + title).
LOGIN_URL_SUBSTRINGS = (
    "/user/login",
    "/user/auth",
    "/login",
    "/sign-in",
    "/signin",
    "/account/login",
)


async def check_login_page(page) -> bool:
    """True if navigation looks like a login / sign-in page."""
    try:
        url = (page.url or "").lower()
        if any(s in url for s in LOGIN_URL_SUBSTRINGS):
            return True
        title = (await page.title()).lower()
        if "sign in" in title or "log in" in title:
            if "shein" in title or "member" in title or "account" in title:
                return True
    except Exception:
        pass
    return False
