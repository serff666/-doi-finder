"""Elsevier Article Retrieval API — the route that never meets Cloudflare.

Why this and not the browser: the ScienceDirect web front end sits behind a
managed challenge that only a human can clear, and clearing it per article is
exactly the chore we are trying to remove. The publisher's own API has no such
gate; it asks for a key instead, once.

What the key buys:
  • application/pdf  — the real publisher PDF, when the key carries the
    entitlement (institutional subscribers, and some open-access keys)
  • text/xml view=FULL — full text for open-access articles even without a
    subscription

A key is free at https://dev.elsevier.com/ (register once, ~2 minutes). It is
sent per request as X-ELS-APIKey and stored in the app's settings, so from then
on every Elsevier DOI resolves without a browser, a challenge or a click.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.elsevier.com/content/article/doi/"
UA = "doi-finder/2.0"
TIMEOUT = 45


def _get(doi: str, key: str, accept: str, view: str = "", token: str = "") -> tuple[int, bytes, str]:
    params = {"httpAccept": accept}
    if view:
        params["view"] = view
    url = API + urllib.parse.quote(doi, safe="/()") + "?" + urllib.parse.urlencode(params)
    headers = {
        "X-ELS-APIKey": key,
        "Accept": accept,
        "User-Agent": UA,
    }
    # insttoken is issued per institution by its library admin; without it the
    # key only reaches open-access full text, not subscription content.
    if token:
        headers["X-ELS-Insttoken"] = token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read(), r.headers.get("content-type") or ""
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:  # noqa: BLE001
            pass
        return e.code, body, e.headers.get("content-type") or "" if e.headers else ""
    except Exception as e:  # noqa: BLE001 - timeout, DNS, TLS
        return 0, str(e).encode(), ""


def _api_error(body: bytes) -> str:
    """Pull the human-readable line out of a service-error envelope."""
    try:
        text = body.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
    for tag in ("statusText", "error-message"):
        marker = f"<{tag}>"
        if marker in text:
            return text.split(marker, 1)[1].split(f"</{tag}>", 1)[0][:160]
    return text[:160].strip()


def is_elsevier(doi: str) -> bool:
    return (doi or "").lower().startswith("10.1016/")


def fetch(doi: str, key: str, token: str = "") -> tuple[bytes | None, str, str]:
    """Return (pdf_bytes, source_label, error). Never raises."""
    if not key:
        return None, "", "未配置 Elsevier API key"
    if not is_elsevier(doi):
        return None, "", "非 Elsevier DOI"

    status, body, ctype = _get(doi, key, "application/pdf", token=token)
    if status == 200 and body[:4] == b"%PDF":
        return body, "Elsevier API", ""
    if status == 200 and body[:4] != b"%PDF":
        # The API sometimes answers 200 with an XML error payload.
        err = _api_error(body) or "返回内容不是 PDF"
    else:
        err = _api_error(body) or f"HTTP {status}"

    # No PDF entitlement: is the article open access with full text instead?
    status_x, body_x, _ = _get(doi, key, "text/xml", view="FULL", token=token)
    if status_x == 200 and (b"<ce:para" in body_x or b"<xocs:doc" in body_x):
        # 没有 PDF 不等于没有全文：把结构化全文交回去，存成 XML，能读、能翻
        return body_x, "Elsevier 全文 XML", ""
    return None, "", err or "API 无返回"
