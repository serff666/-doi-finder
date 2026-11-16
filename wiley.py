"""Wiley TDM API — publisher-authenticated PDF retrieval.

Same idea as elsevier.py: use the publisher's own door instead of fighting the
front end. Wiley's token comes from accepting the click-through TDM licence;
it is bound to the subscriber and must never be shipped with the app.

Rate limits are the tight part: the licence says Wiley conveys them in machine
readable form and expects clients to honour them. We stay conservative —
at most 3 requests/second and 60 per 10 minutes, enforced globally so several
download workers cannot add up to a burst.
"""
from __future__ import annotations

import collections
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.wiley.com/onlinelibrary/tdm/v1/articles/"
UA = "doi-finder/2.0"
TIMEOUT = 90

PER_SECOND = 3
WINDOW_SECONDS = 600
WINDOW_LIMIT = 60

_lock = threading.Lock()
_recent: collections.deque[float] = collections.deque()
_last = [0.0]


def _gate() -> None:
    """Block until this request is inside both the per-second and window budget."""
    with _lock:
        now = time.time()
        while _recent and now - _recent[0] > WINDOW_SECONDS:
            _recent.popleft()
        # per-second spacing
        gap = (1.0 / PER_SECOND) - (now - _last[0])
        if gap > 0:
            time.sleep(gap)
            now = time.time()
        while _recent and now - _recent[0] > WINDOW_SECONDS:
            _recent.popleft()
        if len(_recent) >= WINDOW_LIMIT:
            wait = WINDOW_SECONDS - (now - _recent[0]) + 0.5
            time.sleep(max(0.5, min(wait, 60)))
            now = time.time()
            while _recent and now - _recent[0] > WINDOW_SECONDS:
                _recent.popleft()
        _recent.append(now)
        _last[0] = now


def is_wiley(doi: str) -> bool:
    return (doi or "").lower().startswith("10.1002/")


def fetch(doi: str, token: str) -> tuple[bytes | None, str, str]:
    """Return (pdf_bytes, source_label, error). Never raises."""
    if not token:
        return None, "", "未配置 Wiley TDM token"
    if not is_wiley(doi):
        return None, "", "非 Wiley DOI"

    _gate()
    url = API + urllib.parse.quote(doi, safe="/()")
    req = urllib.request.Request(url, headers={
        "Wiley-TDM-Client-Token": token,
        "User-Agent": UA,
        "Accept": "application/pdf",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = r.read()
        if data[:4] == b"%PDF":
            return data, "Wiley TDM", ""
        return None, "", "返回内容不是 PDF"
    except urllib.error.HTTPError as e:
        if e.code == 403:
            # Wiley answers 403 for anything outside the institution's entitlement
            return None, "", "Wiley 403：该文不在订阅范围内（或 token 已失效）"
        if e.code == 401:
            return None, "", "Wiley 401：token 无效"
        if e.code == 429:
            return None, "", "Wiley 429：超出限流，稍后再试"
        return None, "", f"Wiley HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 - timeouts, DNS, reset
        return None, "", f"Wiley {type(e).__name__}"
