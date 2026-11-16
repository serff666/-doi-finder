"""Shared network etiquette for the metadata APIs.

Why this exists (rules as of 2026):
  * OpenAlex requires a free API key (api_key=...) for real use; `mailto` alone
    no longer buys anything. Without a key the daily quota is a tiny demo budget.
  * Crossref limits by request type: list queries 3 req/s (polite, i.e. with a
    mailto) or 1 req/s (public); single-record lookups 10 req/s polite, 5 public.
    Concurrency is 3 (polite) or 1 (public). Exceeding it gets you blocked.
  * Unpaywall needs a real contact email in every request.

Every module routes its URLs through prepare() and wraps the request in gate().
"""
from __future__ import annotations

import contextlib
import threading
import time
import urllib.parse

_state = {"openalex_key": "", "email": "", "core_key": ""}


def set_openalex_key(key: str) -> None:
    _state["openalex_key"] = (key or "").strip()


def set_core_key(key: str) -> None:
    _state["core_key"] = (key or "").strip()


def core_key() -> str:
    return _state["core_key"]


def set_email(email: str) -> None:
    _state["email"] = (email or "").strip()


def email() -> str:
    return _state["email"]


def _host_path(url: str):
    p = urllib.parse.urlsplit(url)
    return (p.hostname or "").lower(), p.path, p.query


def prepare(url: str, mailto: str = "") -> str:
    """Add api_key (OpenAlex) or mailto (Crossref) when we have them and the URL lacks them."""
    host, _path, query = _host_path(url)
    extra = {}
    if host == "api.openalex.org" and _state["openalex_key"] and "api_key=" not in query:
        extra["api_key"] = _state["openalex_key"]
    elif host == "api.crossref.org" and "mailto=" not in query:
        m = mailto or _state["email"]
        if m:
            extra["mailto"] = m
    if not extra:
        return url
    return url + ("&" if "?" in url else "?") + urllib.parse.urlencode(extra)


class _Gate:
    def __init__(self, per_second: float, concurrency: int):
        self.interval = 1.0 / per_second
        self.sem = threading.Semaphore(concurrency)
        self.lock = threading.Lock()
        self.next_at = 0.0

    @contextlib.contextmanager
    def hold(self):
        self.sem.acquire()
        try:
            with self.lock:
                now = time.monotonic()
                wait = self.next_at - now
                self.next_at = max(now, self.next_at) + self.interval
            if wait > 0:
                time.sleep(wait)
            yield
        finally:
            self.sem.release()


# slightly under the published limits, so jitter never tips us over
_GATES = {
    ("crossref", "list", True): _Gate(2.8, 3),
    ("crossref", "list", False): _Gate(0.9, 1),
    ("crossref", "one", True): _Gate(9.0, 3),
    ("crossref", "one", False): _Gate(4.5, 1),
}


def _is_list(path: str, query: str) -> bool:
    # /works/<doi> is a single record; /works?..., /journals/<issn>/works?... are lists
    parts = [p for p in path.split("/") if p]
    if parts[:1] == ["works"] and len(parts) >= 2:
        return False
    return True


@contextlib.contextmanager
def gate(url: str, mailto: str = ""):
    host, path, query = _host_path(url)
    if host != "api.crossref.org":
        yield
        return
    polite = bool(mailto or _state["email"] or "mailto=" in query)
    kind = "list" if _is_list(path, query) else "one"
    with _GATES[("crossref", kind, polite)].hold():
        yield


def retry_after(err, default: float = 5.0) -> float:
    """Seconds to wait after an HTTP 429/503, honouring Retry-After when present."""
    try:
        v = err.headers.get("Retry-After")
        if v:
            return min(float(v), 60.0)
    except Exception:  # noqa: BLE001
        pass
    return default
