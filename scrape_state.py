"""
SQLite persistence for url_clicker: which PDP URLs finished (and how), plus
``discovered`` product ids (from ``data-id`` on recommendations / carousels) to
scrape in later runs. Safe for multiple parallel processes (WAL, busy timeout).

Last outcomes in ``RETRYABLE_STATUSES`` (blocked, nav_error, save_error) are kept
in ``results`` and merged back into the URL queue on the next run (see
``urls_pending_retry``) so they can be scraped again. Successful rows remove the
corresponding pid from ``discovered``.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

# Non-terminal outcomes: keep in the work queue for a later run (see :meth:`urls_pending_retry`).
RETRYABLE_STATUSES = frozenset({"blocked", "nav_error", "save_error"})


def _open_conn(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path), timeout=60.0, check_same_thread=False, isolation_level=None
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class ScrapeState:
    """
    - ``results``: one row per URL attempt (ok / errors).
    - ``discovered``: pids seen on a PDP (e.g. data-id) not yet successfully scraped.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (Path(__file__).resolve().parent / "scrape_state.sqlite3")
        self._lock = threading.Lock()
        with self._lock:
            conn = _open_conn(self.path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS results (
                        url TEXT PRIMARY KEY,
                        pid TEXT NOT NULL,
                        status TEXT NOT NULL,
                        saved_path TEXT,
                        error_hint TEXT,
                        proxy_label TEXT,
                        updated_ts REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS results_pid_status
                        ON results (pid, status);
                    CREATE TABLE IF NOT EXISTS discovered (
                        pid TEXT PRIMARY KEY,
                        from_pid TEXT,
                        from_url TEXT,
                        created_ts REAL NOT NULL
                    );
                    """
                )
            finally:
                conn.close()

    def _conn(self) -> sqlite3.Connection:
        return _open_conn(self.path)

    def record_result(
        self,
        url: str,
        pid: str,
        status: str,
        *,
        saved_path: str = "",
        error_hint: str = "",
        proxy_label: str = "",
    ) -> None:
        """Insert or replace latest outcome for this URL."""
        now = time.time()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO results (url, pid, status, saved_path, "
                    "error_hint, proxy_label, updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        url,
                        pid,
                        status,
                        saved_path,
                        error_hint,
                        proxy_label,
                        now,
                    ),
                )
                if status == "ok":
                    conn.execute("DELETE FROM discovered WHERE pid = ?", (pid,))
            finally:
                conn.close()

    def success_urls(self) -> set[str]:
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "SELECT url FROM results WHERE status = 'ok'",
                )
                return {r[0] for r in cur.fetchall()}
            finally:
                conn.close()

    def success_pids(self) -> set[str]:
        """Goods ids with last recorded status ``ok`` (stable skip key vs URL query variants)."""
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "SELECT pid FROM results WHERE status = 'ok'",
                )
                return {str(r[0]).strip() for r in cur.fetchall() if str(r[0]).strip()}
            finally:
                conn.close()

    def urls_pending_retry(self) -> list[str]:
        """
        URLs whose last recorded status is retryable (blocked, nav_error, save_error).
        Used to re-append them to the queue on the next run so they are scraped again.
        Order: oldest ``updated_ts`` first.
        """
        statuses = tuple(sorted(RETRYABLE_STATUSES))
        q = ",".join("?" * len(statuses))
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    f"SELECT url FROM results WHERE status IN ({q}) "
                    "ORDER BY updated_ts ASC",
                    statuses,
                )
                return [r[0] for r in cur.fetchall()]
            finally:
                conn.close()

    def add_discovered_pids(
        self,
        pids: list[str],
        *,
        from_pid: str,
        from_url: str,
    ) -> int:
        """
        Queue pids for a future run. Skips the source pid and any already-ok
        pids. Returns number of new rows inserted.
        """
        if not pids:
            return 0
        now = time.time()
        added = 0
        with self._lock:
            conn = self._conn()
            try:
                ok_pids = {
                    r[0]
                    for r in conn.execute("SELECT pid FROM results WHERE status = 'ok'")
                }
                for p in pids:
                    p = str(p).strip()
                    if not p.isdigit() or p == from_pid:
                        continue
                    if p in ok_pids:
                        continue
                    n0 = conn.total_changes
                    conn.execute(
                        "INSERT OR IGNORE INTO discovered (pid, from_pid, from_url, created_ts) "
                        "VALUES (?, ?, ?, ?)",
                        (p, from_pid, from_url, now),
                    )
                    if conn.total_changes > n0:
                        added += 1
            finally:
                conn.close()
        return added

    def pending_scrape_pids(self) -> list[str]:
        """
        Pids in ``discovered`` that are not yet successful in ``results``.
        Ordered by created_ts.
        """
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    """
                    SELECT d.pid FROM discovered d
                    WHERE d.pid NOT IN (SELECT pid FROM results WHERE status = 'ok')
                    ORDER BY d.created_ts
                    """
                )
                return [r[0] for r in cur.fetchall()]
            finally:
                conn.close()

    def wal_checkpoint(self) -> None:
        """Call every few minutes; coalesces WAL (helps parallel readers)."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            finally:
                conn.close()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            conn = self._conn()
            try:
                n_ok = conn.execute(
                    "SELECT COUNT(*) FROM results WHERE status = 'ok'"
                ).fetchone()[0]
                n_err = conn.execute(
                    "SELECT COUNT(*) FROM results WHERE status != 'ok'"
                ).fetchone()[0]
                n_dis = conn.execute("SELECT COUNT(*) FROM discovered").fetchone()[0]
            finally:
                conn.close()
        return {"ok": n_ok, "not_ok": n_err, "discovered_pending": n_dis, "db": str(self.path)}
