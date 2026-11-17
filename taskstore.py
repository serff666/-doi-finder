"""History of tasks, one file per task, so a finished (or half-finished) run can be reopened later.

Every page of the UI saves its results here as it goes: the pasted input, the result rows and the state
of the page's download panel. Reopening a task brings all of it back, including what was downloaded and
what is still missing, so a paper found today can still be filed next month.

Layout: <data dir>/history/<kind>/<id>.json   {id, kind, title, summary, created, updated, payload}
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid

KINDS = ("lookup", "download", "search", "chem", "journal", "rg", "lab", "journal_lib", "preprint", "archive")
KEEP_PER_KIND = 60
MAX_PAYLOAD = 6 * 1024 * 1024          # bytes of JSON per task; the request body limit is 8 MB
_ID = re.compile(r"^[a-z0-9]{8,40}$")


class StoreError(ValueError):
    pass


def _dir(root: str, kind: str) -> str:
    if kind not in KINDS:
        raise StoreError("unknown kind")
    return os.path.join(root, "history", kind)


def _path(root: str, kind: str, tid: str) -> str:
    if not _ID.match(tid or ""):
        raise StoreError("bad id")
    return os.path.join(_dir(root, kind), tid + ".json")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def save(root: str, kind: str, tid: str | None, title: str, summary: str, payload: dict) -> dict:
    """Create or replace a task. Returns its metadata. Raises StoreError for bad input."""
    if not isinstance(payload, dict):
        raise StoreError("payload must be an object")
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > MAX_PAYLOAD:
        raise StoreError("task too large to keep")
    tid = tid or new_id()
    path = _path(root, kind, tid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    now = int(time.time())
    created = now
    try:
        with open(path, encoding="utf-8") as fh:
            created = int(json.load(fh).get("created") or now)
    except Exception:  # noqa: BLE001
        pass
    rec = {"id": tid, "kind": kind, "title": str(title or "")[:200], "summary": str(summary or "")[:300],
           "created": created, "updated": now, "payload": payload}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False)
    os.replace(tmp, path)
    _trim(root, kind)
    return {k: rec[k] for k in ("id", "kind", "title", "summary", "created", "updated")}


def load(root: str, kind: str, tid: str) -> dict | None:
    try:
        with open(_path(root, kind, tid), encoding="utf-8") as fh:
            return json.load(fh)
    except StoreError:
        raise
    except Exception:  # noqa: BLE001
        return None


def listing(root: str, kind: str) -> list[dict]:
    """Metadata only (no payload), newest first."""
    d = _dir(root, kind)
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return []
    for n in names:
        if not n.endswith(".json") or not _ID.match(n[:-5]):
            continue
        try:
            with open(os.path.join(d, n), encoding="utf-8") as fh:
                rec = json.load(fh)
            out.append({k: rec.get(k) for k in ("id", "kind", "title", "summary", "created", "updated")})
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda r: r.get("updated") or 0, reverse=True)
    return out


def delete(root: str, kind: str, tid: str) -> bool:
    try:
        os.remove(_path(root, kind, tid))
        return True
    except StoreError:
        raise
    except OSError:
        return False


def _trim(root: str, kind: str) -> None:
    for r in listing(root, kind)[KEEP_PER_KIND:]:
        try:
            os.remove(_path(root, kind, r["id"]))
        except Exception:  # noqa: BLE001
            pass
